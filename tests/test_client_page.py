"""Погодження кошторису по пунктах і сторінка замовлення для клієнта (/c/<токен>)."""

import base64

import httpx
from fastapi.testclient import TestClient

from tesla_ro.telegram import TelegramAPI, TelegramBot
from tesla_ro.web import create_app

from test_send_client import GROUP, FakeTelegram, private_start, saved

JPEG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 100).decode()


def login(web):
    web.post("/login", data={"password": "secret-pass"})


def test_per_line_approval_on_client_page(tmp_path, session):
    number = saved(session)  # робота «Заміна передніх гальмівних колодок» 72 $ + колодки 150 $
    session.add_operation("HVAC-FILTER")  # + 30 $ робота, + 45 $ фільтр
    session.store.save(session.order)
    fake = FakeTelegram()
    api = TelegramAPI("TOKEN", httpx.Client(transport=httpx.MockTransport(fake.handler)))
    app = create_app(orders_dir=session.store.dir, password="secret-pass",
                     telegram=lambda store, comments: TelegramBot(api, GROUP, store, comments, tmp_path / "tg.json"))
    web, bot = TestClient(app), app.state.telegram
    login(web)
    bot.order_saved(session.store.load(number))
    bot.flush()
    web.post(f"/api/orders/{number}/comments", json={"text": "ВНУТРІШНЄ: клієнт скупий", "author": "Іван"})
    pid = web.post(f"/api/orders/{number}/photos", json={"image": JPEG, "kind": "пошкодження"}).json()[0]["id"]
    hidden = web.post(f"/api/orders/{number}/photos", json={"image": JPEG}).json()[1]["id"]
    web.patch(f"/api/orders/{number}/photos/{pid}", json={"line": "Заміна салонного фільтра"})

    r = web.post(f"/api/orders/{number}/send-approval").json()
    link = r["link"]
    token = link.rsplit("/", 1)[1]
    assert r["telegram"] is None and len(r["approval"]["lines"]) == 4

    client = TestClient(app)  # без входу
    page = client.get(f"/c/{token}")
    assert page.status_code == 200 and "Погодження кошторису" in page.text
    assert "Заміна салонного фільтра" in page.text and "ВНУТРІШНЄ" not in page.text and "Іван" not in page.text
    assert f"/c/{token}/photo/{pid}" in page.text
    assert client.get(f"/c/{token}/photo/{pid}").status_code == 200
    assert client.get(f"/c/{token}/photo/{hidden}").status_code == 404  # не для клієнта
    assert client.get(f"/c/{token}/pdf").content.startswith(b"%PDF")
    assert client.get("/c/wrong-token").status_code == 404
    assert client.get("/api/state").status_code == 401  # решта сайту — як і раніше, лише після входу

    decisions = {"labor:0": "approved", "parts:0": "approved", "labor:1": "deferred", "parts:1": "deferred"}
    assert client.post(f"/c/{token}/approve", json={"decisions": {"labor:0": "так"}}).status_code == 409
    assert client.post(f"/c/{token}/approve", json={"decisions": decisions}).json() == {"status": "partial"}
    assert client.post(f"/c/{token}/approve", json={"decisions": decisions}).status_code == 409  # вдруге — ні
    rec = web.get(f"/api/orders/{number}/extras").json()["approval"]
    assert rec["status"] == "partial" and rec["approved_total"] == "222.00" and rec["deferred_total"] == "75.00"
    note = web.get(f"/api/orders/{number}/comments").json()[-1]["text"]
    assert "погодив 2 з 4 на 222,00 $, відклав: Заміна салонного фільтра (30,00 $)" in note
    bot.flush()
    assert any("погодив 2 з 4" in m["text"] and "на сторінці" in m["text"] for m in fake.sent("sendMessage") if m["chat_id"] == GROUP)
    assert "Погоджено на 222,00 $, решту відкладено" in client.get(f"/c/{token}").text

    # відкладене — із замовлення в рекомендації
    web.post(f"/api/orders/{number}/open")
    order = web.post("/api/order/approval/defer").json()["order"]
    assert [x["name"] for x in order["labor"]] == ["Заміна передніх гальмівних колодок"] and len(order["parts"]) == 1
    assert order["totals"]["total"] == "222.00"
    assert order["recommendations"][-2].startswith("Відкладено клієнтом") and "Заміна салонного фільтра — 30,00 $" in order["recommendations"][-2]
    assert web.post("/api/order/approval/defer").status_code == 400  # уже перенесено


def test_telegram_buttons_include_page_link(tmp_path, session):
    number = saved(session)
    fake = FakeTelegram()
    api = TelegramAPI("TOKEN", httpx.Client(transport=httpx.MockTransport(fake.handler)))
    app = create_app(orders_dir=session.store.dir,
                     telegram=lambda store, comments: TelegramBot(api, GROUP, store, comments, tmp_path / "tg.json"))
    web, bot = TestClient(app), app.state.telegram
    token = web.post(f"/api/orders/{number}/client-link").json()["link"].split("start=")[1]
    bot.handle_update(private_start(token, 777))
    import os
    os.environ["PUBLIC_URL"] = "https://dk.example.com"
    try:
        r = web.post(f"/api/orders/{number}/send-approval").json()
    finally:
        del os.environ["PUBLIC_URL"]
    assert r["telegram"] is True and r["link"].startswith("https://dk.example.com/c/")
    doc = fake.sent("sendDocument")[-1]["multipart"].decode("utf-8", "replace")
    assert '"url"' in doc and r["link"] in doc  # кнопка «🔎 Обрати по пунктах» веде на сторінку
    # «Погоджую все» у Telegram — усі рядки погоджено
    msg_id = r["approval"]["message_id"] or bot.approval(number)["message_id"]
    bot.handle_update({"update_id": 5, "callback_query": {"id": "q", "data": f"ap:{number}:y", "from": {"first_name": "Петро"},
                       "message": {"message_id": msg_id, "chat": {"id": 777, "type": "private"}}}})
    rec = bot.approval(number)
    assert rec["status"] == "approved" and all(x["decision"] == "approved" for x in rec["lines"])
