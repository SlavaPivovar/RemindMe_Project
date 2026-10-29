"""Акт приймання автомобіля."""

import base64
import io

import httpx
from fastapi.testclient import TestClient

from tesla_ro.telegram import TelegramAPI, TelegramBot
from tesla_ro.web import create_app

from test_send_client import GROUP, FakeTelegram, private_start, saved


def png() -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (60, 20), "white").save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


JPEG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 100).decode()


def test_intake_sign_and_print(session):
    number = saved(session)
    web = TestClient(create_app(orders_dir=session.store.dir))
    assert "Ключ-карта" in web.get("/api/state").json()["intake_items"]
    web.post(f"/api/orders/{number}/open")
    r = web.put("/api/order/intake", json={"items": ["Ключ-карта", "Ключ-карта", " Килимки "],
                                           "left_inside": "сумка", "damages": "подряпина на бампері"}).json()
    assert r["order"]["intake"]["items"] == ["Ключ-карта", "Килимки"] and r["unsigned"] is False
    assert web.post("/api/order/intake/sign", json={"image": png(), "signed_by": ""}).status_code == 400
    assert web.post("/api/order/intake/sign", json={"image": JPEG, "signed_by": "Петро"}).status_code == 400
    assert web.post("/api/order/intake/sign", json={"image": "data:image/png;base64,!!!", "signed_by": "Петро"}).status_code == 400
    act = web.post("/api/order/intake/sign", json={"image": png(), "signed_by": "Петренко Петро"}).json()["order"]["intake"]
    assert act["signed_by"] == "Петренко Петро" and act["signature"].startswith(number)
    assert web.get(f"/signatures/{act['signature']}").content.startswith(b"\x89PNG")
    assert web.get("/signatures/..%2Fx.png").status_code == 404
    assert session.store.load(number).intake["signature"] == act["signature"]  # збережено одразу

    # фото «до»/«пошкодження» потрапляють в акт, «після» — ні
    web.post(f"/api/orders/{number}/photos", json={"image": JPEG, "kind": "пошкодження"})
    web.post(f"/api/orders/{number}/photos", json={"image": JPEG, "kind": "після"})
    html = web.get(f"/print/{number}/act").text
    assert "АКТ ПРИЙМАННЯ АВТОМОБІЛЯ" in html and "☑ Ключ-карта" in html and "☐ Дитяче крісло" in html
    assert "подряпина на бампері" in html and html.count("<figure>") == 1 and "data:image/png;base64" in html
    pdf = web.get(f"/pdf/{number}/act")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert web.get("/print/ЗН-2026-09999/act").status_code == 404

    # однаковий зміст — підпис лишається; змінили — підпис знято
    same = web.put("/api/order/intake", json={"items": ["Ключ-карта", "Килимки"], "left_inside": "сумка",
                                              "damages": "подряпина на бампері"}).json()
    assert same["unsigned"] is False and same["order"]["intake"]["signature"]
    changed = web.put("/api/order/intake", json={"items": ["Ключ-карта"], "left_inside": "", "damages": ""}).json()
    assert changed["unsigned"] is True and "signature" not in changed["order"]["intake"]
    assert web.put("/api/order/intake", json={"damages": "x" * 1001}).status_code == 400


def test_signed_act_is_sent_to_client(tmp_path, session):
    number = saved(session)
    fake = FakeTelegram()
    api = TelegramAPI("TOKEN", httpx.Client(transport=httpx.MockTransport(fake.handler)))
    app = create_app(orders_dir=session.store.dir,
                     telegram=lambda store, comments: TelegramBot(api, GROUP, store, comments, tmp_path / "tg.json"))
    web, bot = TestClient(app), app.state.telegram
    token = web.post(f"/api/orders/{number}/client-link").json()["link"].split("start=")[1]
    bot.handle_update(private_start(token, 777))
    before = len(fake.sent("sendDocument"))
    web.post(f"/api/orders/{number}/send-telegram")
    assert len(fake.sent("sendDocument")) == before + 1  # акта ще немає — лише замовлення-наряд
    web.post(f"/api/orders/{number}/open")
    web.put("/api/order/intake", json={"items": ["Ключ-карта"]})
    web.post("/api/order/intake/sign", json={"image": png(), "signed_by": "Петренко Петро"})
    web.post(f"/api/orders/{number}/send-telegram")
    docs = fake.sent("sendDocument")
    assert len(docs) == before + 3 and "Акт приймання" in docs[-1]["multipart"].decode("utf-8", "replace")
