"""Розпізнавання фото та статуси у веб-інтерфейсі."""

import base64
import json

import anthropic
import httpx2
import pytest
from anthropic import DefaultHttpxClient
from fastapi.testclient import TestClient

from tesla_ro.vision import PhotoError, clean_vin, parse_data_url, summarize
from tesla_ro.web import create_app

JPEG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff fake jpeg").decode()


def reply_client(payload: dict, calls: list):
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": json.dumps(payload)}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})
    return anthropic.Anthropic(api_key="test", http_client=DefaultHttpxClient(transport=httpx2.MockTransport(handler)))


def test_parse_data_url():
    assert parse_data_url(JPEG)[0] == "image/jpeg"
    with pytest.raises(PhotoError, match="JPEG"):
        parse_data_url("data:image/heic;base64,AAAA")
    with pytest.raises(PhotoError, match="Некоректне"):
        parse_data_url("not an image")
    big = "data:image/png;base64," + base64.b64encode(b"0" * (5 * 1024 * 1024 + 1)).decode()
    with pytest.raises(PhotoError, match="завелике"):
        parse_data_url(big)


def test_clean_vin_fixes_ocr_letters():
    assert clean_vin(" 7sa ygd eexpf0o0oo1 ") == "7SAYGDEEXPF000001"
    assert clean_vin("") is None


def test_summarize_vin_and_miles():
    r = summarize({"vin": "7SAYGDEEXPF000001", "odometer_value": 38000, "odometer_unit": "mi", "notes": ""})
    assert r["odometer_km"] == 61155 and r["vin_warnings"] == []
    assert r["text"] == "З фото: VIN 7SAYGDEEXPF000001 (Model Y 2023), пробіг 61 155 км (на фото 38 000 миль)."


def test_summarize_warns():
    r = summarize({"vin": "5YJ3E1EA1LF000316", "odometer_value": 120, "odometer_unit": "unknown", "notes": "розмите"})
    assert any("Контрольна цифра" in w for w in r["warnings"])
    assert any("км це чи милі" in w for w in r["warnings"])
    assert summarize({"vin": None, "odometer_value": None, "odometer_unit": "unknown", "notes": "темно"})["text"] == ""


def test_photo_endpoint(tmp_path):
    calls = []
    client = reply_client({"vin": "5YJ3E1EA9LF000316", "odometer_value": 48210, "odometer_unit": "km",
                           "notes": ""}, calls)
    web = TestClient(create_app(orders_dir=tmp_path, client=client))
    r = web.post("/api/photo", json={"image": JPEG}).json()
    assert r["text"] == "З фото: VIN 5YJ3E1EA9LF000316 (Model 3 2020), пробіг 48 210 км."
    sent = calls[0]
    assert sent["messages"][0]["content"][0]["type"] == "image"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert web.post("/api/photo", json={"image": "data:text/plain;base64,AA=="}).status_code == 400


def test_status_endpoint_and_filter(tmp_path, session):
    web = TestClient(create_app(orders_dir=session.store.dir))
    assert web.post("/api/order/status", json={"status": "готово"}).status_code == 404
    session.new_order()
    session.set_customer("Іваненко Іван", "+380670000000")
    session.set_vehicle("5YJ3E1EA9LF000316", odometer_km=1000)
    session.add_complaint("скарга")
    session.add_operation("DIAG-01")
    number = session.order.number
    web.post(f"/api/orders/{number}/open")
    r = web.post("/api/order/status", json={"status": "в роботі"})
    assert r.status_code == 200 and r.json()["order"]["status"] == "в роботі"
    assert r.json()["order"]["status_log"][-1]["status"] == "в роботі"
    assert session.store.load(number).status == "в роботі"  # одразу збережено
    bad = web.post("/api/order/status", json={"status": "щось"})
    assert bad.status_code == 400 and "Невідомий" in bad.json()["error"]
    assert [o["number"] for o in web.get("/api/orders", params={"status": "в роботі"}).json()] == [number]
    assert web.get("/api/orders", params={"status": "видано"}).json() == []
    assert "в роботі" in web.get("/api/state").json()["statuses"]
