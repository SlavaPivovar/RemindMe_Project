"""Співробітники: вхід кожного під своїм ім'ям, ролі, прив'язка Telegram."""

import re

import httpx
import pytest
from fastapi.testclient import TestClient

from tesla_ro.staff import StaffError, StaffStore
from tesla_ro.telegram import TelegramAPI, TelegramBot
from tesla_ro.web import create_app

from test_send_client import GROUP, FakeTelegram, saved

PWD = "secret-pass"


def test_store_codes_passwords_and_invites(tmp_path):
    st = StaffStore(tmp_path / "staff.json")
    ivan = st.create({"name": " Іван  Петров ", "phone": "+380 67 111 22 33", "role": "master"})
    assert ivan["name"] == "Іван Петров" and ivan["invite"].startswith("s_")
    with pytest.raises(StaffError):
        st.create({"name": "Дубль", "phone": "067 111 22 33", "role": "admin"})  # той самий номер
    with pytest.raises(StaffError):
        st.create({"name": "Хтось", "phone": "12", "role": "master"})
    with pytest.raises(StaffError):
        st.request_code("0671112233")  # Telegram ще не прив'язано
    bound = st.bind_telegram(ivan["invite"], 555, "Іван")
    assert bound["tg_id"] == 555 and st.bind_telegram(ivan["invite"], 556, "x") is None  # запрошення одноразове
    e, code = st.request_code("067 111 22 33")
    with pytest.raises(StaffError):
        st.request_code("0671112233")  # не частіше ніж раз на 45 с
    assert st.verify_code("0671112233", "000000" if code != "000000" else "111111") is None
    assert st.verify_code("+380671112233", code)["id"] == ivan["id"]
    assert st.verify_code("+380671112233", code) is None  # код одноразовий

    with pytest.raises(StaffError):
        st.set_password(ivan["id"], "short")
    st.set_password(ivan["id"], "long-password")
    assert st.login_password("0671112233", "long-password")["id"] == ivan["id"]
    assert st.login_password("0671112233", "wrong-password") is None
    ver = st.get(ivan["id"])["ver"]
    st.update(ivan["id"], {"active": False})
    assert st.get(ivan["id"])["ver"] == ver + 1 and st.login_password("0671112233", "long-password") is None
    assert StaffStore(tmp_path / "staff.json").list()[0]["pwd"].startswith("pbkdf2$")  # пароль — лише хеш


def app_with(tmp_path, session, telegram=True):
    fake = FakeTelegram()
    api = TelegramAPI("TOKEN", httpx.Client(transport=httpx.MockTransport(fake.handler)))
    factory = (lambda store, comments: TelegramBot(api, GROUP, store, comments, tmp_path / "tg.json")) if telegram else None
    app = create_app(orders_dir=session.store.dir, password=PWD, staff_path=tmp_path / "staff.json", telegram=factory)
    return fake, app


def test_roles_and_password_login(tmp_path, session):
    number = saved(session)
    _, app = app_with(tmp_path, session, telegram=False)
    owner = TestClient(app)
    assert owner.post("/login", data={"password": PWD}, follow_redirects=False).status_code == 303
    assert owner.get("/api/state").json()["user"]["role"] == "owner"
    m = owner.post("/api/staff", json={"name": "Іван", "phone": "+380671112233", "role": "master"}).json()["staff"][0]
    a = owner.post("/api/staff", json={"name": "Олена", "phone": "+380501112233", "role": "admin"}).json()["staff"][1]
    assert "pwd" not in m and not m["has_password"]
    owner.put(f"/api/staff/{m['id']}/password", json={"password": "master-pass"})
    owner.put(f"/api/staff/{a['id']}/password", json={"password": "admin-pass1"})

    master = TestClient(app)
    bad = master.post("/login", data={"phone": "067 111 22 33", "password": "wrong-pass"}, follow_redirects=False)
    assert bad.headers["location"].startswith("/login?error=1")
    master.post("/login", data={"phone": "067 111 22 33", "password": "master-pass"})
    me = master.get("/api/state").json()["user"]
    assert me["name"] == "Іван" and me["role_label"] == "майстер"
    master.post(f"/api/orders/{number}/open")
    # майстер: замовлення можна, гроші й налаштування — ні
    assert master.post("/api/order/complaints", json={"text": "Ще скрип"}).status_code == 200
    c = master.post(f"/api/orders/{number}/comments", json={"text": "Зробив", "author": "Хтось інший"}).json()[-1]
    assert c["author"] == "Іван"  # ім'я з входу, не підробити
    st = master.post("/api/order/status", json={"status": "в роботі", "by": "Не я"}).json()["order"]["status_log"][-1]
    assert st["by"] == "Іван"
    for method, url in (("post", "/api/order/payments"), ("get", "/api/cash"), ("get", "/api/debts"),
                        ("delete", f"/api/orders/{number}"), ("get", "/api/staff"), ("put", "/api/catalog/settings"),
                        ("get", "/api/backup.zip"), ("get", "/api/purchases"), ("post", "/api/catalog/operations")):
        r = getattr(master, method)(url, **({"json": {}} if method in ("post", "put") else {}))
        assert r.status_code == 403, (method, url)
    assert "майстер" not in master.get("/api/cash").json()["error"] and "адміністратор" in master.get("/api/cash").json()["error"]
    d = master.get("/api/dashboard").json()["kpi"]
    assert d["cash_today"] is None and d["debts"] is None
    assert master.get("/api/state").json()["order"]["margin"] is None
    assert not [x for x in master.get("/api/dashboard").json()["alerts"] if x["kind"] == "debt"]

    admin = TestClient(app)
    admin.post("/login", data={"phone": "+380501112233", "password": "admin-pass1"})
    admin.post(f"/api/orders/{number}/open")
    assert admin.post("/api/order/payments", json={"amount": "10", "method": "готівка"}).status_code == 200
    assert admin.get("/api/cash").json()["payments"][0]["by"] == "Олена"
    assert admin.get("/api/staff").status_code == 403 and admin.get("/api/dashboard").json()["kpi"]["cash_today"] == "10.00"

    # вимкнули чи змінили роль — вхід на всіх пристроях недійсний
    owner.put(f"/api/staff/{m['id']}", json={"active": False})
    assert master.get("/api/state").status_code == 401
    owner.put(f"/api/staff/{a['id']}", json={"role": "master"})
    assert admin.get("/api/state").status_code == 401


def test_telegram_invite_and_code_login(tmp_path, session):
    fake, app = app_with(tmp_path, session)
    bot = app.state.telegram
    owner = TestClient(app)
    owner.post("/login", data={"password": PWD})
    staff = owner.post("/api/staff", json={"name": "Іван", "phone": "+380671112233", "role": "owner"}).json()["staff"]
    link = staff[0]["invite_link"]
    assert link.startswith("https://t.me/dk_test_bot?start=s_")

    def private(text, uid=555):
        return {"update_id": 1, "message": {"message_id": 1, "chat": {"id": uid, "type": "private"},
                                            "from": {"id": uid, "first_name": "Ivan"}, "text": text}}
    bot.handle_update(private("/start " + link.split("start=")[1]))
    assert "Ви підключені як власник" in fake.sent("sendMessage")[-1]["text"]
    assert owner.get("/api/staff").json()["staff"][0]["tg_id"] == 555

    web = TestClient(app)
    assert web.post("/login/code", json={"phone": "0500000000"}).status_code == 400
    r = web.post("/login/code", json={"phone": "067 111 22 33"})
    assert r.status_code == 200 and r.json()["name"] == "Іван"
    msg = fake.sent("sendMessage")[-1]
    assert msg["chat_id"] == 555
    code = re.search(r"<b>(\d{6})</b>", msg["text"]).group(1)
    assert web.post("/login/verify", json={"phone": "0671112233", "code": "12345"}).status_code == 400
    assert web.post("/login/verify", json={"phone": "0671112233", "code": code}).status_code == 200
    assert web.get("/api/state").json()["user"]["name"] == "Іван"

    # власник у списку — підсумок і копії йому в Telegram без BACKUP_CHAT_ID
    assert bot.owner_chats() == [555]
    bot.handle_update(private("/today"))
    assert "Підсумок дня" in fake.sent("sendMessage")[-1]["text"]

    # у групі тепер працюють лише співробітники
    number = saved(session)
    bot.order_saved(session.store.load(number))
    bot.flush()
    stranger = {"id": 999, "first_name": "Чужий"}
    bot.handle_update({"update_id": 2, "message": {"message_id": 7, "chat": {"id": GROUP, "type": "supergroup"},
                                                   "from": stranger, "text": "/orders"}})
    assert "не зареєстровані" in fake.sent("sendMessage")[-1]["text"]
    bot.handle_update({"update_id": 3, "callback_query": {"id": "q", "data": f"me:{number}", "from": stranger,
                                                          "message": {"message_id": 1, "chat": {"id": GROUP}}}})
    assert "не зареєстровані" in fake.sent("answerCallbackQuery")[-1]["text"]
    bot.handle_update({"update_id": 4, "callback_query": {"id": "q2", "data": f"me:{number}", "from": {"id": 555, "first_name": "Ivan"},
                                                          "message": {"message_id": 1, "chat": {"id": GROUP}}}})
    assert session.store.load(number).assignee == "Іван"  # ім'я зі списку співробітників, не з Telegram
