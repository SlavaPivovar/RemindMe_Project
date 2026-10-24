"""Тесты веб-интерфейса с подменённым API Anthropic."""

import json

import anthropic
import httpx2
from anthropic import DefaultHttpxClient
from fastapi.testclient import TestClient

from tesla_ro.web import create_app


def _msg(content, stop_reason):
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _tool(name, args, i):
    return {"type": "tool_use", "id": f"tu_{i}", "name": name, "input": args}


SCRIPT = [
    _msg([
        _tool("new_order", {}, 1),
    ], "tool_use"),
    _msg([
        _tool("set_customer", {"name": "Иванов Иван", "phone": "+79000000000"}, 2),
        _tool("set_vehicle", {"vin": "5YJ3E1EA9LF000316", "odometer_km": 48000}, 3),
        _tool("add_complaint", {"text": "Скрип при торможении"}, 4),
        _tool("add_operation", {"code": "BRK-PAD-F"}, 5),
    ], "tool_use"),
    _msg([_tool("save_order", {}, 6)], "tool_use"),
    _msg([{"type": "text", "text": "Заказ-наряд **сохранён**."}], "end_turn"),
]


def make_client(responses):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx2.Response(200, json=responses[len(calls) - 1])

    client = anthropic.Anthropic(api_key="test", http_client=DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    return client, calls


def test_full_flow(tmp_path):
    client, _ = make_client(SCRIPT)
    web = TestClient(create_app(orders_dir=tmp_path, client=client))

    state = web.get("/api/state").json()
    assert state["order"] is None and state["history"] == []
    assert "tesla_ro_sid" in web.cookies

    r = web.post("/api/chat", json={"text": "Иванов, VIN 5YJ3E1EA9LF000316, скрип"}).json()
    assert r["reply"] == "Заказ-наряд **сохранён**."
    order = r["order"]
    assert order["vehicle"]["model"] == "Model 3"
    assert order["status"] == "открыт" and order["missing_fields"] == []
    assert order["totals"]["total"] == "22300.00"

    # история и заказ восстанавливаются после перезагрузки страницы
    state = web.get("/api/state").json()
    assert [m["role"] for m in state["history"]] == ["user", "assistant"]
    assert state["order"]["number"] == order["number"]

    assert "ЗАКАЗ-НАРЯД" in web.get("/print/current").text
    saved = web.get("/api/orders").json()
    assert [o["number"] for o in saved] == [order["number"]]
    assert "Иванов Иван" in web.get(f"/print/{order['number']}").text

    assert web.post("/api/reset").json() == {"order": None}
    assert web.get("/api/state").json()["order"] is None
    opened = web.post(f"/api/orders/{order['number']}/open").json()
    assert opened["order"]["number"] == order["number"]


def test_sessions_are_isolated(tmp_path):
    client, _ = make_client(SCRIPT)
    app = create_app(orders_dir=tmp_path, client=client)
    a, b = TestClient(app), TestClient(app)
    a.post("/api/chat", json={"text": "заказ"})
    assert a.get("/api/state").json()["order"] is not None
    assert b.get("/api/state").json()["order"] is None


def test_errors(tmp_path):
    def handler(request):
        return httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}})

    client = anthropic.Anthropic(api_key="bad", max_retries=0,
                                 http_client=DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    web = TestClient(create_app(orders_dir=tmp_path, client=client))
    r = web.post("/api/chat", json={"text": "привет"})
    assert r.status_code == 503 and "ключ" in r.json()["error"]
    assert web.post("/api/chat", json={"text": "  "}).status_code == 400
    assert web.get("/print/current").status_code == 404
    assert web.get("/print/..%2F..%2Fetc").status_code == 404
    assert web.post("/api/orders/ЗН-2026-99999/open").status_code == 404


def test_index_served(tmp_path):
    client, _ = make_client([])
    r = TestClient(create_app(orders_dir=tmp_path, client=client)).get("/")
    assert r.status_code == 200 and "Заказ-наряды Tesla" in r.text


def test_password_required(tmp_path, monkeypatch):
    monkeypatch.setattr("tesla_ro.web.time.sleep", lambda s: None)
    client, _ = make_client(SCRIPT)
    web = TestClient(create_app(orders_dir=tmp_path, client=client, password="secret-pass"))

    assert web.get("/api/state").status_code == 401
    assert web.get("/api/orders").status_code == 401
    r = web.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert web.get("/print/ЗН-2026-00001", follow_redirects=False).status_code == 303
    assert "Введите пароль" in web.get("/login").text

    r = web.post("/login", data={"password": "wrong"}, follow_redirects=False)
    assert r.headers["location"] == "/login?error=1"
    assert "Неверный пароль" in web.get("/login?error=1").text
    assert web.get("/api/state").status_code == 401

    web.post("/login", data={"password": "secret-pass"})
    state = web.get("/api/state").json()
    assert state["auth"] is True
    assert web.post("/api/chat", json={"text": "заказ"}).status_code == 200

    web.get("/logout")
    assert web.get("/api/state").status_code == 401


def test_forged_auth_cookie_rejected(tmp_path):
    client, _ = make_client([])
    web = TestClient(create_app(orders_dir=tmp_path, client=client, password="secret-pass"))
    web.cookies.set("tesla_ro_auth", "9999999999.deadbeef")
    assert web.get("/api/state").status_code == 401


def test_login_rate_limited(tmp_path, monkeypatch):
    monkeypatch.setattr("tesla_ro.web.time.sleep", lambda s: None)
    client, _ = make_client([])
    web = TestClient(create_app(orders_dir=tmp_path, client=client, password="secret-pass"))
    for _ in range(10):
        web.post("/login", data={"password": "wrong"}, follow_redirects=False)
    r = web.post("/login", data={"password": "secret-pass"}, follow_redirects=False)
    assert r.status_code == 429


def test_no_password_mode_is_open(tmp_path):
    client, _ = make_client([])
    web = TestClient(create_app(orders_dir=tmp_path, client=client))
    assert web.get("/api/state").json()["auth"] is False
