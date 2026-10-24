"""Перевірка циклу діалогу з підміненим HTTP-транспортом (без реального API)."""

import json

import anthropic
import httpx2
from anthropic import DefaultHttpxClient

from tesla_ro.assistant import RepairOrderAssistant


def _msg(content, stop_reason):
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def test_tool_loop_and_history(session):
    requests = []
    replies = [
        _msg([{"type": "tool_use", "id": "tu_1", "name": "new_order", "input": {}}], "tool_use"),
        _msg([{"type": "text", "text": "Створено замовлення-наряд."}], "end_turn"),
        _msg([{"type": "text", "text": "Готово."}], "end_turn"),
    ]

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(200, json=replies[len(requests) - 1])

    client = anthropic.Anthropic(
        api_key="test", http_client=DefaultHttpxClient(transport=httpx2.MockTransport(handler))
    )
    assistant = RepairOrderAssistant(session, client=client)

    assert assistant.send("Нове замовлення") == "Створено замовлення-наряд."
    assert session.order is not None
    assert requests[0]["model"] == "claude-opus-5-5"
    assert requests[0]["fallbacks"] == "default"
    tool_result = requests[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and session.order.number in json.dumps(tool_result["content"], ensure_ascii=False)

    assert assistant.send("Дякую") == "Готово."
    roles = [m["role"] for m in requests[2]["messages"]]
    assert roles == ["user", "assistant", "user", "assistant", "user"]


def test_history_rolled_back_on_error(session):
    def handler(request):
        return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}})

    client = anthropic.Anthropic(
        api_key="test", max_retries=0, http_client=DefaultHttpxClient(transport=httpx2.MockTransport(handler))
    )
    assistant = RepairOrderAssistant(session, client=client)
    try:
        assistant.send("привіт")
    except anthropic.BadRequestError:
        pass
    assert assistant.messages == []
