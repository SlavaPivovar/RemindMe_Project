"""Запис на сервіс: планувальник, помічник запису, Telegram."""

import json
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from tesla_ro import bookings as bk
from tesla_ro.telegram import TelegramAPI, TelegramBot
from tesla_ro.web import create_app

from test_send_client import GROUP, FakeTelegram, private_start, saved
from test_web import _msg, _tool, make_client

TOMORROW = (date.today() + timedelta(days=1)).isoformat()


def test_store_validation_overlaps_and_moves(tmp_path):
    store = bk.BookingStore(tmp_path / "bookings.json")
    events = []
    store.listeners.append(lambda kind, b: events.append((kind, b["id"])))
    a = store.create({"date": TOMORROW, "time": "10:00", "client_name": "Петренко", "work": "ТО"}, by="Іван")
    assert a["duration_min"] == 60 and a["status"] == bk.PLANNED and a["created_by"] == "Іван"
    b = store.create({"date": TOMORROW, "time": "10.30", "client_name": "Коваль", "duration_min": 30})
    assert b["time"] == "10:30" and [x["id"] for x in store.overlaps(b)] == [a["id"]]
    with pytest.raises(bk.BookingError):
        store.create({"date": "завтра", "time": "10:00", "client_name": "x"})
    with pytest.raises(bk.BookingError):
        store.create({"date": TOMORROW, "time": "25:00", "client_name": "x"})
    with pytest.raises(bk.BookingError):
        store.create({"date": TOMORROW, "time": "10:00", "client_name": " "})

    before, after = store.update(a["id"], {"time": "12:00"}, by="Олег")
    assert before["time"] == "10:00" and after["time"] == "12:00" and after["history"][0]["from"].endswith("10:00")
    assert store.overlaps(store.get(b["id"])) == []
    store.set_status(b["id"], bk.CANCELLED, by="Олег")
    assert [x["id"] for x in store.list(TOMORROW, TOMORROW)] == [a["id"]]
    assert len(store.list(TOMORROW, TOMORROW, include_cancelled=True)) == 2
    with pytest.raises(bk.BookingError):
        store.update(b["id"], {"time": "13:00"})
    assert [k for k, _ in events] == ["created", "created", "updated", "cancelled"]
    # переживає перезапуск
    assert bk.BookingStore(tmp_path / "bookings.json").get(a["id"])["time"] == "12:00"


def test_agent_creates_booking_from_free_text(tmp_path, session):
    saved(session)  # клієнт Петренко Петро вже є в базі
    client, calls = make_client([
        _msg([_tool("find_client", {"query": "Петренко"}, 1)], "tool_use"),
        _msg([_tool("create_booking", {"date": TOMORROW, "time": "10:00", "client_name": "Петренко Петро",
                                       "phone": "+380 67 123 45 67", "car": "Tesla Model Y",
                                       "work": "ТО і стукіт спереду"}, 2)], "tool_use"),
        _msg([{"type": "text", "text": "Записав Петренка Петра на завтра о 10:00."}], "end_turn"),
        _msg([{"type": "text", "text": "Так, завтра о 10:00."}], "end_turn"),
    ])
    web = TestClient(create_app(orders_dir=session.store.dir, client=client, usage_path=tmp_path / "u.jsonl"))
    r = web.post("/api/bookings/agent", json={"text": "Петренко завтра о 10, ТО і стукіт спереду", "author": "Іван"})
    assert r.json()["reply"] == "Записав Петренка Петра на завтра о 10:00."
    system = calls[0]["system"]
    system = system if isinstance(system, str) else " ".join(x["text"] for x in system)
    assert date.today().isoformat() in system and "Сьогодні" in system
    found = json.loads(calls[1]["messages"][-1]["content"][0]["content"])
    assert found[0]["phone"] == "+380 67 123 45 67" and "Model Y" in found[0]["cars"][0]
    day = web.get(f"/api/bookings?start={TOMORROW}&days=1").json()["days"][0]
    b = day["items"][0]
    assert b["client_name"] == "Петренко Петро" and b["created_by"] == "Іван" and b["source"] == "web"
    # друга репліка продовжує ту саму розмову
    web.post("/api/bookings/agent", json={"text": "підтверди", "author": "Іван"})
    assert len(calls[3]["messages"]) > 1
    assert '"kind": "booking"' in (tmp_path / "u.jsonl").read_text(encoding="utf-8")

    # клієнт приїхав — замовлення-наряд створюється лише за кнопкою
    order = web.post(f"/api/bookings/{b['id']}/order").json()["order"]
    assert order["customer"]["name"] == "Петренко Петро" and order["vehicle"]["vin"] == "7SAYGDEEXPF000001"
    assert order["complaints"] == ["ТО і стукіт спереду"]
    b = web.get(f"/api/bookings?start={TOMORROW}&days=1").json()["days"][0]["items"][0]
    assert b["status"] == bk.ARRIVED and b["order_number"] == order["number"]
    assert web.post(f"/api/bookings/{b['id']}/order").status_code == 409


def test_manual_crud(session):
    web = TestClient(create_app(orders_dir=session.store.dir))
    r = web.post("/api/bookings", json={"date": TOMORROW, "time": "09:00", "client_name": "Коваль", "author": "Іван"})
    bid = r.json()["booking"]["id"]
    assert web.post("/api/bookings", json={"date": TOMORROW, "time": "", "client_name": "x"}).status_code == 400
    r = web.put(f"/api/bookings/{bid}", json={"time": "11:00", "work": "шини"}).json()["booking"]
    assert r["time"] == "11:00" and r["work"] == "шини" and r["client_name"] == "Коваль"
    assert web.post(f"/api/bookings/{bid}/status", json={"status": "щось"}).status_code == 400
    assert web.post(f"/api/bookings/{bid}/status", json={"status": "не приїхав"}).json()["booking"]["status"] == "не приїхав"
    assert web.put("/api/bookings/nope", json={"time": "11:00"}).status_code == 404
    week = web.get("/api/bookings").json()
    assert len(week["days"]) == 7 and week["days"][0]["date"] == date.today().isoformat()


def test_telegram_sync_and_client_notifications(tmp_path, session):
    number = saved(session)
    fake = FakeTelegram()
    api = TelegramAPI("TOKEN", httpx.Client(transport=httpx.MockTransport(fake.handler)))
    client, calls = make_client([
        _msg([_tool("list_bookings", {"date_from": TOMORROW}, 1)], "tool_use"),
        _msg([{"type": "text", "text": "Готово"}], "end_turn"),
    ])
    app = create_app(orders_dir=session.store.dir, client=client, bookings_path=tmp_path / "b.json",
                     telegram=lambda store, comments: TelegramBot(api, GROUP, store, comments, tmp_path / "tg.json"))
    web, bot = TestClient(app), app.state.telegram
    token = web.post(f"/api/orders/{number}/client-link").json()["link"].split("start=")[1]
    bot.handle_update(private_start(token, 777))

    bid = web.post("/api/bookings", json={"date": TOMORROW, "time": "10:00", "client_name": "Петренко Петро",
                                          "phone": "067 123 45 67", "work": "ТО", "author": "Іван"}).json()["booking"]["id"]
    bot.flush()
    group = [m for m in fake.sent("sendMessage") if m["chat_id"] == GROUP]
    assert "📅 <b>Запис</b>" in group[-1]["text"] and "Петренко Петро" in group[-1]["text"]
    to_client = [m["text"] for m in fake.sent("sendMessage") if m["chat_id"] == 777]
    assert to_client[-1].startswith("📅 Вас записано") and "о 10:00" in to_client[-1]

    web.put(f"/api/bookings/{bid}", json={"time": "12:00"})
    bot.flush()
    assert any("🔁 Перенесено" in m["text"] and "12:00" in m["text"] for m in fake.sent("sendMessage") if m["chat_id"] == GROUP)
    assert "перенесено на" in [m["text"] for m in fake.sent("sendMessage") if m["chat_id"] == 777][-1]
    assert "12:00" in fake.sent("editMessageText")[-1]["text"]

    web.post(f"/api/bookings/{bid}/status", json={"status": "скасовано"})
    bot.flush()
    assert "скасовано" in [m["text"] for m in fake.sent("sendMessage") if m["chat_id"] == 777][-1]
    assert "<s>" in fake.sent("editMessageText")[-1]["text"]

    # /zapys без тексту — розклад на сьогодні й завтра; з текстом — помічник
    msg = {"message_id": 70, "chat": {"id": GROUP, "type": "supergroup"}, "from": {"id": 5, "first_name": "Іван"}}
    bot.handle_update({"update_id": 1, "message": {**msg, "text": "/zapys"}})
    assert "Сьогодні" in fake.sent("sendMessage")[-1]["text"] and "Завтра" in fake.sent("sendMessage")[-1]["text"]
    bot.handle_update({"update_id": 2, "message": {**msg, "text": "/zapys хто записаний на завтра?"}})
    assert fake.sent("sendMessage")[-1]["text"] == "Готово"
    assert "[Іван]: хто записаний на завтра?" in json.dumps(calls[0]["messages"], ensure_ascii=False)
