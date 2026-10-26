"""Надсилання замовлення-наряду клієнту: PDF і Telegram-бот."""

import json

import httpx
from fastapi.testclient import TestClient

from tesla_ro.telegram import TelegramAPI, TelegramBot
from tesla_ro.web import create_app

GROUP = -100123


class FakeTelegram:
    def __init__(self):
        self.calls, self.next_id = [], 500

    def handler(self, request):
        method = request.url.path.rsplit("/", 1)[-1]
        if request.headers.get("content-type", "").startswith("multipart/"):
            body = {"multipart": request.content}
        else:
            body = json.loads(request.content or b"{}")
        self.calls.append((method, body))
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "dk_test_bot"}})
        if method in ("sendMessage", "sendDocument"):
            self.next_id += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_id}})
        return httpx.Response(200, json={"ok": True, "result": True})

    def sent(self, method):
        return [b for m, b in self.calls if m == method]


def app_with_bot(tmp_path, session):
    fake = FakeTelegram()
    api = TelegramAPI("TOKEN", httpx.Client(transport=httpx.MockTransport(fake.handler)))
    app = create_app(orders_dir=session.store.dir,
                     telegram=lambda store, comments: TelegramBot(api, GROUP, store, comments, tmp_path / "tg.json"))
    return fake, TestClient(app), app.state.telegram


def saved(session, phone="+380 67 123 45 67", status="прийнято"):
    session.new_order()
    session.set_customer("Петренко Петро", phone)
    session.set_vehicle("7SAYGDEEXPF000001", odometer_km=62300)
    session.add_complaint("Скрип")
    session.add_operation("BRK-PAD-F")
    session.save_order(status)
    return session.order.number


def private_start(token, chat_id=777, message_id=1):
    return {"update_id": message_id, "message": {"message_id": message_id, "chat": {"id": chat_id, "type": "private"},
            "text": f"/start {token}", "from": {"id": chat_id, "first_name": "Петро", "username": "petro"}}}


def test_pdf_endpoints(session):
    number = saved(session)
    web = TestClient(create_app(orders_dir=session.store.dir))
    r = web.get(f"/pdf/{number}")
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and r.headers["content-type"] == "application/pdf"
    assert "filename*=UTF-8''" in r.headers["content-disposition"]
    assert web.get("/pdf/ЗН-2026-09999").status_code == 404
    assert web.get("/pdf/current").status_code == 404
    web.post(f"/api/orders/{number}/open")
    assert web.get("/pdf/current").content.startswith(b"%PDF")
    # без бота — зрозуміла помилка
    assert web.post(f"/api/orders/{number}/client-link").status_code == 503


def test_client_connects_and_gets_pdf(tmp_path, session):
    number = saved(session)
    fake, web, bot = app_with_bot(tmp_path, session)
    bot.order_saved(session.store.load(number))  # замовлення збережено до запуску бота — публікуємо картку
    bot.flush()

    r = web.post(f"/api/orders/{number}/client-link").json()
    assert r["link"].startswith("https://t.me/dk_test_bot?start=o_") and r["client"] is None and r["has_phone"]
    assert r["qr_svg"].startswith("<svg")
    assert web.post(f"/api/orders/{number}/client-link").json()["link"] == r["link"]  # те саме посилання
    assert web.post(f"/api/orders/{number}/send-telegram").status_code == 409  # клієнт ще не підключився

    token = r["link"].split("start=")[1]
    bot.handle_update(private_start(token))
    doc = fake.sent("sendDocument")[-1]["multipart"]
    assert b'name="chat_id"\r\n\r\n777' in doc and b"%PDF" in doc
    assert any("Вітаємо" in m["text"] and m["chat_id"] == 777 for m in fake.sent("sendMessage"))
    assert "клієнт підключився" in fake.sent("sendMessage")[-1]["text"]  # позначка в групі під карткою

    assert web.post(f"/api/orders/{number}/client-link").json()["client"] == "Петро (@petro)"
    # наступне замовлення того ж клієнта (телефон в іншому форматі) — одним натисканням
    number2 = saved(session, phone="067 123 45 67")
    r2 = web.post(f"/api/orders/{number2}/send-telegram")
    assert r2.status_code == 200 and r2.json() == {"sent": True, "client": "Петро (@petro)", "photos": 0}
    assert len(fake.sent("sendDocument")) == 2


def test_private_chat_edge_cases(tmp_path, session):
    number = saved(session, phone="", status="чернетка")
    fake, web, bot = app_with_bot(tmp_path, session)
    bot.handle_update(private_start("o_wrong"))
    assert "застаріло" in fake.sent("sendMessage")[-1]["text"]
    bot.handle_update({"update_id": 2, "message": {"message_id": 2, "chat": {"id": 5, "type": "private"},
                                                   "text": "привіт", "from": {"id": 5, "first_name": "X"}}})
    assert "бот сервісу" in fake.sent("sendMessage")[-1]["text"]
    r = web.post(f"/api/orders/{number}/client-link").json()
    assert r["has_phone"] is False
    bot.handle_update(private_start(r["link"].split("start=")[1]))  # без телефону: PDF надсилаємо, але не запам'ятовуємо
    assert len(fake.sent("sendDocument")) == 1 and bot.links["clients"] == {}
    # приватні повідомлення не стають коментарями до замовлень
    assert bot.comments.count(number) == 0
