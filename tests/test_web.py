"""Тести веб-інтерфейсу з підміненим API Anthropic."""

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
        _tool("set_customer", {"name": "Іваненко Іван", "phone": "+380670000000"}, 2),
        _tool("set_vehicle", {"vin": "5YJ3E1EA9LF000316", "odometer_km": 48000}, 3),
        _tool("add_complaint", {"text": "Скрип під час гальмування"}, 4),
        _tool("add_operation", {"code": "BRK-PAD-F"}, 5),
    ], "tool_use"),
    _msg([_tool("save_order", {}, 6)], "tool_use"),
    _msg([{"type": "text", "text": "Замовлення-наряд **збережено**."}], "end_turn"),
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

    r = web.post("/api/chat", json={"text": "Іваненко, VIN 5YJ3E1EA9LF000316, скрип"}).json()
    assert r["reply"] == "Замовлення-наряд **збережено**."
    order = r["order"]
    assert order["vehicle"]["model"] == "Model 3"
    assert order["status"] == "відкрито" and order["missing_fields"] == []
    assert order["totals"]["total"] == "222.00"

    # історія та замовлення відновлюються після перезавантаження сторінки
    state = web.get("/api/state").json()
    assert [m["role"] for m in state["history"]] == ["user", "assistant"]
    assert state["order"]["number"] == order["number"]

    assert "ЗАМОВЛЕННЯ-НАРЯД" in web.get("/print/current").text
    saved = web.get("/api/orders").json()
    assert [o["number"] for o in saved] == [order["number"]]
    assert "Іваненко Іван" in web.get(f"/print/{order['number']}").text

    assert web.post("/api/reset").json() == {"order": None}
    assert web.get("/api/state").json()["order"] is None
    opened = web.post(f"/api/orders/{order['number']}/open").json()
    assert opened["order"]["number"] == order["number"]


def test_sessions_are_isolated(tmp_path):
    client, _ = make_client(SCRIPT)
    app = create_app(orders_dir=tmp_path, client=client)
    a, b = TestClient(app), TestClient(app)
    a.post("/api/chat", json={"text": "замовлення"})
    assert a.get("/api/state").json()["order"] is not None
    assert b.get("/api/state").json()["order"] is None


def test_errors(tmp_path):
    def handler(request):
        return httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}})

    client = anthropic.Anthropic(api_key="bad", max_retries=0,
                                 http_client=DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    web = TestClient(create_app(orders_dir=tmp_path, client=client))
    r = web.post("/api/chat", json={"text": "привіт"})
    assert r.status_code == 503 and "ключ" in r.json()["error"]
    assert web.post("/api/chat", json={"text": "  "}).status_code == 400
    assert web.get("/print/current").status_code == 404
    assert web.get("/print/..%2F..%2Fetc").status_code == 404
    assert web.post("/api/orders/ЗН-2026-99999/open").status_code == 404


def test_index_served(tmp_path):
    client, _ = make_client([])
    r = TestClient(create_app(orders_dir=tmp_path, client=client)).get("/")
    assert r.status_code == 200 and "DK-TeslaService" in r.text


def test_password_required(tmp_path, monkeypatch):
    monkeypatch.setattr("tesla_ro.web.time.sleep", lambda s: None)
    client, _ = make_client(SCRIPT)
    web = TestClient(create_app(orders_dir=tmp_path, client=client, password="secret-pass"))

    assert web.get("/api/state").status_code == 401
    assert web.get("/api/orders").status_code == 401
    r = web.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert web.get("/print/ЗН-2026-00001", follow_redirects=False).status_code == 303
    login = web.get("/login").text
    assert "введіть пароль" in login and "<h1>DK-TeslaService</h1>" in login

    r = web.post("/login", data={"password": "wrong"}, follow_redirects=False)
    assert r.headers["location"] == "/login?error=1"
    assert "Невірний пароль" in web.get("/login?error=1").text
    assert web.get("/api/state").status_code == 401

    web.post("/login", data={"password": "secret-pass"})
    state = web.get("/api/state").json()
    assert state["auth"] is True
    assert web.post("/api/chat", json={"text": "замовлення"}).status_code == 200

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


def test_catalog_endpoint(tmp_path):
    client, _ = make_client([])
    web = TestClient(create_app(orders_dir=tmp_path, client=client))
    data = web.get("/api/catalog").json()
    assert "Гальмівна система" in data["categories"] and data["currency"] == "$"
    pads = next(o for o in data["operations"] if o["code"] == "BRK-PAD-F")
    assert pads["labor_price"] == 72 and pads["parts_price"] == 150 and pads["models"] == "усі"
    filt = next(o for o in data["operations"] if o["code"] == "HVAC-FILTER")
    assert filt["models"] == "Model 3, Model Y" and filt["interval"] == "кожні 2 роки"
