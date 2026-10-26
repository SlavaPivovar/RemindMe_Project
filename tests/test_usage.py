"""Облік витрат на Claude."""

import base64
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import anthropic
import httpx2
from anthropic import DefaultHttpxClient
from fastapi.testclient import TestClient

from tesla_ro.usage import UsageLedger, from_response
from tesla_ro.web import create_app

USAGE = {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 5000,
         "cache_creation_input_tokens": 0}
COST = 0.015  # 1000×$4 + 500×$20 + 5000×$0.20 за 1 млн


def msg(content, stop_reason, usage=USAGE):
    return {"id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": stop_reason, "stop_sequence": None, "usage": usage}


def test_prices():
    u = SimpleNamespace(**USAGE)
    assert from_response(u, "claude-opus-5-5")["cost_usd"] == COST
    assert from_response(u, "claude-sonnet-5-5")["cost_usd"] == 0.008
    assert from_response(u, "claude-unknown")["cost_usd"] == COST  # невідома модель — за ціною Opus 5.5
    w = SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
                        cache_creation_input_tokens=1_000_000)
    assert from_response(w, "claude-opus-5-5")["cost_usd"] == 5.0


def test_ledger_summary(tmp_path):
    path = tmp_path / "usage.jsonl"
    ledger = UsageLedger(path)
    ledger.record("chat", {"requests": 2, "cost_usd": 0.10}, "m", "ЗН-1")
    ledger.record("photo", {"requests": 1, "cost_usd": 0.02}, "m", "ЗН-2")
    ledger.record("chat", {"requests": 0, "cost_usd": 0}, "m", None)  # порожнє не пишеться
    old = (datetime.now() - timedelta(days=40)).isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": old, "order": "ЗН-0", "requests": 1, "cost_usd": 1.0}) + "\n")
        f.write("{обірваний рядок\n")
    s = UsageLedger(path).summary()
    assert s["today"]["cost_usd"] == 0.12 and s["today"]["requests"] == 3
    assert s["all"]["cost_usd"] == 1.12 and s["orders"] == 3
    assert s["avg_per_order"] == round(1.12 / 3, 4)
    assert len(s["by_day"]) == 14 and s["by_day"][-1]["cost_usd"] == 0.12


def test_chat_and_photo_costs_go_to_order(tmp_path):
    script = [
        msg([{"type": "tool_use", "id": "t1", "name": "new_order", "input": {}}], "tool_use"),
        msg([{"type": "tool_use", "id": "t2", "name": "set_customer",
              "input": {"name": "Іваненко Іван", "phone": "+380670000000"}}], "tool_use"),
        msg([{"type": "text", "text": "Готово."}], "end_turn"),
    ]
    photo = {"vin": "5YJ3E1EA9LF000316", "odometer_value": 1000, "odometer_unit": "km", "notes": ""}
    chat_calls = []

    def handler(request):
        body = json.loads(request.content)
        if "format" in body.get("output_config", {}):
            return httpx2.Response(200, json=msg([{"type": "text", "text": json.dumps(photo)}], "end_turn"))
        chat_calls.append(body)
        return httpx2.Response(200, json=script[len(chat_calls) - 1])

    client = anthropic.Anthropic(api_key="t", http_client=DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    usage_path = tmp_path / "usage.jsonl"
    web = TestClient(create_app(orders_dir=tmp_path / "orders", client=client, usage_path=usage_path))

    image = "data:image/jpeg;base64," + base64.b64encode(b"jpeg").decode()
    web.post("/api/photo", json={"image": image})  # фото до появи замовлення — чекає на нього
    order = web.post("/api/chat", json={"text": "нове замовлення"}).json()["order"]
    assert order["ai_usage"]["requests"] == 4  # фото + 3 запити ходу
    assert round(order["ai_usage"]["cost_usd"], 6) == round(COST * 4, 6)

    saved = json.loads((tmp_path / "orders" / f"{order['number']}.json").read_text(encoding="utf-8"))
    assert saved["ai_usage"]["requests"] == 4  # збережено на диск
    listed = web.get("/api/orders").json()[0]
    assert round(listed["ai_cost"], 6) == round(COST * 4, 6)

    lines = [json.loads(x) for x in usage_path.read_text(encoding="utf-8").splitlines()]
    assert [(x["kind"], x["requests"], x["order"]) for x in lines] == [("photo", 1, None), ("chat", 3, order["number"])]
    summary = web.get("/api/usage").json()
    assert summary["today"]["requests"] == 4 and summary["orders"] == 1
    assert summary["avg_per_order"] == round(COST * 4, 4)  # разом із фото, зробленим до замовлення
    printed = web.get("/print/current").text
    assert "Іваненко Іван" in printed and "Claude" not in printed and "0,06" not in printed  # клієнт витрат не бачить


def test_cost_counted_even_if_turn_fails(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx2.Response(200, json=msg([{"type": "tool_use", "id": "t1", "name": "new_order",
                                                    "input": {}}], "tool_use"))
        return httpx2.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "boom"}})

    client = anthropic.Anthropic(api_key="t", max_retries=0,
                                 http_client=DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    usage_path = tmp_path / "usage.jsonl"
    web = TestClient(create_app(orders_dir=tmp_path / "orders", client=client, usage_path=usage_path))
    web.get("/api/state")  # як і сторінка: спершу стан (ставить cookie сесії)
    assert web.post("/api/chat", json={"text": "привіт"}).status_code == 502
    state = web.get("/api/state").json()
    assert state["order"]["ai_usage"]["requests"] == 1  # перший запит відбувся і коштував грошей
    assert json.loads(usage_path.read_text(encoding="utf-8"))["requests"] == 1
