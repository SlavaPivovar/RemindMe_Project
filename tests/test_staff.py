"""Кнопки під карткою, відповідальний майстер, голосові коментарі, підсумок дня."""

import json

import httpx
from fastapi.testclient import TestClient

from tesla_ro.telegram import TelegramAPI, TelegramBot
from tesla_ro.web import create_app

from test_send_client import GROUP, FakeTelegram, saved

OWNER = 4242
OGG = b"OggS" + b"\0" * 100
IVAN = {"id": 11, "first_name": "Іван"}


class Fake(FakeTelegram):
    def handler(self, request):
        if "/file/" in request.url.path:
            return httpx.Response(200, content=OGG)
        if request.url.path.endswith("getFile"):
            self.calls.append(("getFile", json.loads(request.content)))
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/file_1.oga"}})
        return super().handler(request)


def make(tmp_path, session):
    fake = Fake()
    api = TelegramAPI("TOKEN", httpx.Client(transport=httpx.MockTransport(fake.handler)))

    def factory(store, comments):
        bot = TelegramBot(api, GROUP, store, comments, tmp_path / "tg.json")
        bot.backup_chat_id = OWNER
        return bot

    app = create_app(orders_dir=session.store.dir, telegram=factory)
    return fake, TestClient(app), app.state.telegram


def press(bot, data, sender=IVAN, chat=GROUP, qid="q"):
    bot.handle_update({"update_id": 1, "callback_query": {"id": qid, "data": data, "from": sender,
                                                          "message": {"message_id": 1, "chat": {"id": chat}}}})


def group_msg(text, **extra):
    return {"update_id": 2, "message": {"message_id": 50, "chat": {"id": GROUP, "type": "supergroup"},
                                        "from": IVAN, "text": text, **extra}}


def publish(bot, session, number):
    bot.order_saved(session.store.load(number))
    bot.flush()
    return bot.links["orders"][number]["message_id"]


def test_status_buttons_and_assignee(tmp_path, session):
    number = saved(session)
    fake, web, bot = make(tmp_path, session)
    publish(bot, session, number)
    card = fake.sent("sendMessage")[-1]
    buttons = [b["callback_data"] for b in card["reply_markup"]["inline_keyboard"][0]]
    assert buttons == [f"st:{number}:2", f"me:{number}"]  # «в роботі» і «Беру собі»

    # відкриваємо замовлення на сайті — зміна з Telegram не повинна потім затертися автозбереженням
    web.post(f"/api/orders/{number}/open")
    press(bot, f"st:{number}:2")
    order = session.store.load(number)
    assert order.status == "в роботі" and order.status_log[-1]["by"] == "Іван"
    assert fake.sent("answerCallbackQuery")[-1]["text"] == "Статус: в роботі"
    press(bot, f"me:{number}")
    bot.flush()
    assert session.store.load(number).assignee == "Іван"
    edited = fake.sent("editMessageText")[-1]
    assert "Відповідальний: Іван" in edited["text"]
    assert edited["reply_markup"]["inline_keyboard"][0][0]["text"] == "✅ Готово"
    group = [m["text"] for m in fake.sent("sendMessage") if m["chat_id"] == GROUP]
    assert any("статус «<b>в роботі</b>» · Іван" in t for t in group)
    assert any("відповідальний — <b>Іван</b>" in t for t in group)

    web.post("/api/order/complaints", json={"text": "Ще стукіт"})  # автозбереження з сайту
    order = session.store.load(number)
    assert order.status == "в роботі" and order.assignee == "Іван" and "Ще стукіт" in order.complaints
    extras = web.get(f"/api/orders/{number}/extras").json()
    assert extras["status"] == "в роботі" and extras["assignee"] == "Іван"

    # повторне «Беру собі» і кнопка з чужого чату нічого не змінюють
    press(bot, f"me:{number}")
    assert "вже відповідальний" in fake.sent("answerCallbackQuery")[-1]["text"]
    press(bot, f"st:{number}:3", chat=999)
    assert session.store.load(number).status == "в роботі"

    # /my
    bot.handle_update(group_msg("/my"))
    assert number in fake.sent("sendMessage")[-1]["text"]
    bot.handle_update({"update_id": 3, "message": {"message_id": 51, "chat": {"id": GROUP, "type": "supergroup"},
                                                   "from": {"id": 12, "first_name": "Петро"}, "text": "/my"}})
    assert "немає замовлень" in fake.sent("sendMessage")[-1]["text"]

    # на сайті: призначити іншого й змінити статус від свого імені
    r = web.put("/api/order/assignee", json={"name": "  Олег  "}).json()["order"]
    assert r["assignee"] == "Олег" and r["assignee_tg"] is None
    r = web.post("/api/order/status", json={"status": "готово", "by": "Олег"}).json()["order"]
    assert r["status_log"][-1] == {"status": "готово", "at": r["status_log"][-1]["at"], "by": "Олег"}
    assert web.get("/api/state").json()["assignees"] == ["Олег"]


def test_status_button_reports_missing_fields(tmp_path, session):
    number = saved(session)
    fake, web, bot = make(tmp_path, session)
    publish(bot, session, number)
    order = session.store.load(number)
    order.vehicle.odometer_km = None
    session.store.save(order)
    press(bot, f"st:{number}:2")
    answer = fake.sent("answerCallbackQuery")[-1]
    assert answer["show_alert"] and "пробіг" in answer["text"]
    assert session.store.load(number).status == "прийнято"


def test_voice_comment(tmp_path, session):
    number = saved(session)
    fake, web, bot = make(tmp_path, session)
    card = publish(bot, session, number)
    bot.handle_update(group_msg("", reply_to_message={"message_id": card},
                                voice={"file_id": "v1", "duration": 83, "mime_type": "audio/ogg"}))
    comment = web.get(f"/api/orders/{number}/comments").json()[-1]
    assert comment["text"] == "🎤 Голосове повідомлення 1:23" and comment["voice"]["duration"] == 83
    r = web.get(f"/voice/{number}/{comment['voice']['file']}")
    assert r.status_code == 200 and r.content == OGG and r.headers["content-type"] == "audio/ogg"
    assert web.get(f"/voice/{number}/../../etc.ogg").status_code == 404
    assert web.get(f"/voice/{number}/aaaaaaaaaaaa.ogg").status_code == 404
    # голосове без прив'язки до замовлення — ігнорується
    bot.handle_update(group_msg("", voice={"file_id": "v2", "duration": 3}))
    assert len(web.get(f"/api/orders/{number}/comments").json()) == 1


def test_daily_summary(tmp_path, session):
    saved(session)  # прийнято сьогодні
    session.save_order("готово")
    saved(session, phone="+380 50 000 00 01")
    session.save_order("видано")
    fake, web, bot = make(tmp_path, session)
    text = bot.daily_summary()
    assert "📥 Прийнято: 2" in text and "✅ Готово: 1" in text  # друге видали, минаючи «готово»
    assert "🏁 Видано: 1 · на суму <b>222,00 $</b>" in text and "Зараз у роботі: 1" in text

    # підсумок раз на добу, лише після 20:00 (перевіряємо, підмінивши годину)
    import tesla_ro.telegram as tg
    old = tg.SUMMARY_HOUR
    tg.SUMMARY_HOUR = 0
    try:
        bot.summary_tick()
        bot.summary_tick()
    finally:
        tg.SUMMARY_HOUR = old
    sent = [m for m in fake.sent("sendMessage") if m["chat_id"] == OWNER]
    assert len(sent) == 1 and "Підсумок дня" in sent[0]["text"]

    # /today — лише власнику
    def private(chat):
        return {"update_id": 9, "message": {"message_id": 9, "chat": {"id": chat, "type": "private"},
                                            "from": {"id": chat}, "text": "/today"}}
    bot.handle_update(private(5))
    assert "лише власнику" in fake.sent("sendMessage")[-1]["text"]
    bot.handle_update(private(OWNER))
    assert "Підсумок дня" in fake.sent("sendMessage")[-1]["text"]


def test_stale_orders_in_summary(tmp_path, session):
    number = saved(session)
    session.save_order("в роботі")
    order = session.store.load(number)
    order.status_log[-1]["at"] = "01.01.2020 10:00"
    order.assignee = "Іван"
    session.store.save(order)
    fake, web, bot = make(tmp_path, session)
    text = bot.daily_summary()
    assert "Довго в роботі" in text and number in text and "👤 Іван" in text
