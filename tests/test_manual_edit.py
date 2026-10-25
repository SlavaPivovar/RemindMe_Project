"""Ручне редагування рядків збереженого замовлення-наряду."""

import json

import anthropic
import httpx2
from anthropic import DefaultHttpxClient
from fastapi.testclient import TestClient

from tesla_ro.web import create_app


def saved_order(session):
    session.new_order()
    session.set_customer("Іваненко Іван", "+380670000000")
    session.set_vehicle("5YJ3E1EA9LF000316", odometer_km=1000)  # Model 3
    session.add_complaint("скарга")
    session.add_operation("DIAG-01")
    session.save_order("видано")
    return session.order.number


def client_recording(calls):
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx2.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": "Добре."}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}})
    return anthropic.Anthropic(api_key="t", http_client=DefaultHttpxClient(transport=httpx2.MockTransport(handler)))


def test_edit_saved_order(session):
    number = saved_order(session)
    calls = []
    web = TestClient(create_app(orders_dir=session.store.dir, client=client_recording(calls)))
    assert web.post("/api/order/labor", json={"code": "DIAG-01"}).status_code == 404  # нічого не відкрито
    web.post(f"/api/orders/{number}/open")

    # з довідника, разом із типовими запчастинами
    order = web.post("/api/order/labor", json={"code": "BRK-PAD-F", "qty": 1}).json()["order"]
    assert [l["code"] for l in order["labor"]] == ["DIAG-01", "BRK-PAD-F"]
    assert [p["part_no"] for p in order["parts"]] == ["PAD-F"]
    # власні позиції
    web.post("/api/order/labor", json={"name": "Мийка", "price": 15})
    web.post("/api/order/parts", json={"name": "Килимки", "price": 40, "qty": 2, "part_no": "MAT-1", "unit": "компл"})
    web.post("/api/order/parts", json={"part_no": "WIPER-SET", "qty": 1})
    # зміна та видалення
    order = web.patch("/api/order/labor/1", json={"price": 50, "discount_percent": 10}).json()["order"]
    assert (order["labor"][0]["price"], order["labor"][0]["discount_percent"]) == (50, 10)
    order = web.delete("/api/order/parts/1").json()["order"]
    assert [p["name"] for p in order["parts"]] == ["Килимки", "Щітки склоочисника, комплект"]
    assert order["totals"]["total"] == "262.00"  # роботи 45 + 72 + 15, запчастини 80 + 50
    saved = session.store.load(number)
    assert [l.name for l in saved.parts] == ["Килимки", "Щітки склоочисника, комплект"]  # автозбереження

    # помилки
    assert web.post("/api/order/labor", json={"code": "SUSP-AIR-SX"}).status_code == 400  # не для Model 3
    assert web.post("/api/order/labor", json={"name": "Без ціни"}).status_code == 400
    assert web.patch("/api/order/labor/9", json={"qty": 2}).status_code == 400
    assert web.patch("/api/order/labor/1", json={"qty": 0}).status_code == 400
    assert web.delete("/api/order/other/1").status_code == 404

    # помічник дізнається про ручні правки з наступним повідомленням, а в історії чату їх не видно
    web.post("/api/chat", json={"text": "Покажи підсумок"})
    sent = calls[0]["messages"][-1]["content"]
    assert sent.startswith("[Зміни, внесені майстром вручну") and "додав роботу «Мийка»" in sent
    assert "видалив «Колодки гальмівні передні, комплект»" in sent and sent.endswith("Покажи підсумок")
    assert web.get("/api/state").json()["history"][0]["text"] == "Покажи підсумок"
    web.post("/api/chat", json={"text": "Дякую"})
    assert calls[-1]["messages"][-1]["content"] == "Дякую"  # уже передано — вдруге не дублюється
