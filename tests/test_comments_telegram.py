"""Внутрішні коментарі та Telegram-бот (з підміненим Telegram API)."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tesla_ro.comments import CommentError, CommentStore
from tesla_ro.telegram import TelegramAPI, TelegramBot
from tesla_ro.web import create_app

GROUP = -100123


class FakeTelegram:
    """Запам'ятовує виклики Bot API і повертає правдоподібні відповіді."""

    def __init__(self):
        self.calls, self.updates, self.next_id, self.fail = [], [], 1000, False

    def handler(self, request):
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        if self.fail:
            raise httpx.ConnectError("offline")
        self.calls.append((method, body))
        if method == "sendMessage":
            self.next_id += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_id}})
        if method == "getUpdates":
            updates, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": updates})
        return httpx.Response(200, json={"ok": True, "result": True})

    def sent(self, method="sendMessage"):
        return [b for m, b in self.calls if m == method]


def make(tmp_path, session):
    fake = FakeTelegram()
    api = TelegramAPI("TOKEN", httpx.Client(transport=httpx.MockTransport(fake.handler)))
    bot = TelegramBot(api, GROUP, session.store, CommentStore(session.store.dir), tmp_path / "telegram.json")
    session.store.listeners.append(bot.order_saved)
    session.comments = bot.comments
    return fake, bot


def fill(session):
    session.new_order()
    session.set_customer("Петренко Петро", "+380671234567")
    session.set_vehicle("7SAYGDEEXPF000001", odometer_km=62300, plate="АА7777КХ")
    session.add_complaint("Скрип спереду")
    session.add_operation("BRK-PAD-F")


def msg(text, message_id, reply_to=None, chat=GROUP, user=None):
    m = {"message_id": message_id, "chat": {"id": chat}, "text": text,
         "from": user or {"id": 7, "first_name": "Олег", "last_name": "Коваль", "username": "oleg_k"}}
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": message_id, "message": m}


def test_comment_store(tmp_path):
    store = CommentStore(tmp_path)
    store.add("ЗН-2026-00001", "Олег", "  Замінили колодки  ")
    assert [c["text"] for c in store.list("ЗН-2026-00001")] == ["Замінили колодки"]
    assert store.list("../etc") == [] and store.count("ЗН-2026-00002") == 0
    with pytest.raises(CommentError):
        store.add("ЗН-2026-00001", "Олег", "   ")
    with pytest.raises(CommentError):
        store.add("bad", "Олег", "текст")


def test_web_comments_not_printed(session):
    fill(session)
    number = session.order.number
    web = TestClient(create_app(orders_dir=session.store.dir))
    assert web.post("/api/orders/ЗН-2026-09999/comments", json={"text": "x"}).status_code == 404
    listed = web.post(f"/api/orders/{number}/comments", json={"text": "Замінили колодки, диски в нормі",
                                                              "author": "Олег"}).json()
    assert listed[0]["author"] == "Олег" and listed[0]["source"] == "web"
    assert web.post(f"/api/orders/{number}/comments", json={"text": "  "}).status_code == 400
    opened = web.post(f"/api/orders/{number}/open").json()["order"]
    assert opened["comments"][0]["text"] == "Замінили колодки, диски в нормі"
    assert web.get("/api/orders").json()[0]["comments"] == 1
    assert "диски в нормі" not in web.get(f"/print/{number}").text  # лише для внутрішнього використання


def test_bot_posts_cards_and_status(tmp_path, session):
    fake, bot = make(tmp_path, session)
    fill(session)  # чернетка
    bot.flush()
    assert fake.calls == []  # чернетки в групу не потрапляють
    session.save_order("прийнято")
    bot.flush()
    card = fake.sent()[0]
    assert card["chat_id"] == GROUP and "ЗН-" in card["text"] and "прийнято" in card["text"]
    assert "Tesla Model Y 2023" in card["text"] and "Заміна передніх гальмівних колодок" in card["text"]
    card_id = fake.next_id

    session.order.set_status("в роботі")
    session.store.save(session.order)
    bot.flush()
    assert fake.sent("editMessageText")[-1]["message_id"] == card_id
    status_msg = fake.sent()[-1]
    assert "в роботі" in status_msg["text"] and status_msg["reply_parameters"]["message_id"] == card_id

    bot.flush()  # нічого не змінилось — нічого не надсилаємо
    assert len(fake.calls) == 3


def test_comments_both_ways(tmp_path, session):
    fake, bot = make(tmp_path, session)
    fill(session)
    session.save_order("прийнято")
    number = session.order.number
    bot.flush()
    card_id = fake.next_id

    # з сайту → у групу відповіддю під карткою
    comment = bot.comments.add(number, "Ірина", "Клієнт просив подзвонити", "web")
    bot.comment_added(number, comment)
    bot.flush()
    posted = fake.sent()[-1]
    assert "Ірина" in posted["text"] and posted["reply_parameters"]["message_id"] == card_id
    assert "Коментарів: 1" in fake.sent("editMessageText")[-1]["text"]

    # відповідь у групі → коментар до замовлення + 👍
    bot.handle_update(msg("Замінили колодки, перевірили диски", 50, reply_to=card_id))
    saved = bot.comments.list(number)[-1]
    assert saved == {**saved, "author": "Олег Коваль (@oleg_k)", "text": "Замінили колодки, перевірили диски",
                     "source": "telegram"}
    assert fake.sent("setMessageReaction")[-1]["message_id"] == 50
    # відповідь на відповідь — теж до цього замовлення
    bot.handle_update(msg("І мастило на супорти", 51, reply_to=50))
    # повідомлення з номером на початку
    bot.handle_update(msg(f"{number}: машина готова", 52))
    assert [c["text"] for c in bot.comments.list(number)][-2:] == ["І мастило на супорти", "машина готова"]

    count = bot.comments.count(number)
    bot.handle_update(msg("просто розмова в групі", 53))  # не коментар
    bot.handle_update(msg("ЗН-2026-99999 немає такого", 54))
    assert "не знайдено" in fake.sent()[-1]["text"]
    bot.handle_update(msg("чужий чат", 55, reply_to=card_id, chat=-999))  # чужі чати ігноруємо
    bot.handle_update(msg("бот", 56, reply_to=card_id, user={"id": 1, "is_bot": True, "first_name": "Bot"}))
    assert bot.comments.count(number) == count


def test_commands(tmp_path, session):
    fake, bot = make(tmp_path, session)
    fill(session)
    session.save_order("в роботі")
    number = session.order.number
    bot.comments.add(number, "Олег", "Чекаємо запчастину")
    bot.handle_update(msg("/chatid", 1, chat=555))  # працює в будь-якому чаті — для налаштування
    assert fake.sent()[-1] == {"chat_id": 555, "text": "ID цього чату: 555"}
    bot.handle_update(msg("/orders@dk_bot", 2))
    assert number in fake.sent()[-1]["text"] and "в роботі" in fake.sent()[-1]["text"]
    bot.handle_update(msg(f"/order {number.lower()}", 3))
    detail = fake.sent()[-1]["text"]
    assert "Чекаємо запчастину" in detail and "Олег" in detail
    # на картку з /order теж можна відповісти коментарем
    bot.handle_update(msg("Запчастина приїхала", 4, reply_to=fake.next_id))
    assert bot.comments.list(number)[-1]["text"] == "Запчастина приїхала"


def test_delete_and_retry_and_polling(tmp_path, session):
    fake, bot = make(tmp_path, session)
    fill(session)
    session.save_order("прийнято")
    number = session.order.number
    fake.fail = True
    bot.flush()  # мережа недоступна — не падаємо, спробуємо пізніше
    fake.fail = False
    bot.flush()
    assert len(fake.sent()) == 1
    bot.order_deleted(number)
    bot.flush()
    assert "видалено" in fake.sent()[-1]["text"]

    card_id = fake.sent() and fake.next_id - 1
    fake.updates = [msg("коментар через polling", 70, reply_to=card_id)]
    bot.poll_once(timeout=0)
    assert bot.comments.list(number)[-1]["text"] == "коментар через polling"
    reloaded = TelegramBot(bot.api, GROUP, session.store, bot.comments, tmp_path / "telegram.json")
    assert reloaded.links["offset"] == 71 and reloaded.number_for_message(card_id) == number


def test_web_app_wires_bot(tmp_path, session):
    fake = FakeTelegram()
    api = TelegramAPI("TOKEN", httpx.Client(transport=httpx.MockTransport(fake.handler)))
    app = create_app(orders_dir=session.store.dir,
                     telegram=lambda store, comments: TelegramBot(api, GROUP, store, comments, tmp_path / "tg.json"))
    web, bot = TestClient(app), app.state.telegram
    assert web.get("/api/state").json()["telegram"] is True
    web.post("/api/order/new")
    web.put("/api/order/customer", json={"name": "Петренко Петро", "phone": "+380671234567"})
    web.put("/api/order/vehicle", json={"vin": "7SAYGDEEXPF000001", "odometer_km": 1000})
    web.post("/api/order/complaints", json={"text": "скрип"})
    web.post("/api/order/labor", json={"code": "DIAG-01"})
    number = web.post("/api/order/status", json={"status": "прийнято"}).json()["order"]["number"]
    web.post(f"/api/orders/{number}/comments", json={"text": "Почали діагностику", "author": "Олег"})
    bot.flush()
    texts = [b["text"] for b in fake.sent()]
    assert any(number in t and "прийнято" in t for t in texts) and any("Почали діагностику" in t for t in texts)
    web.delete(f"/api/orders/{number}")
    bot.flush()
    assert "видалено" in fake.sent()[-1]["text"]


def test_comment_waits_for_card_when_offline(tmp_path, session):
    fake, bot = make(tmp_path, session)
    fill(session)
    session.save_order("прийнято")
    number = session.order.number
    bot.comment_added(number, bot.comments.add(number, "Ірина", "Перший коментар"))
    fake.fail = True
    bot.flush()
    fake.fail = False
    bot.flush()
    texts = [b["text"] for b in fake.sent()]
    assert number in texts[0] and "Перший коментар" in texts[1]
