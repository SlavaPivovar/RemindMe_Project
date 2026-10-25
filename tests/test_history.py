"""Автозбереження чернеток та історія автомобіля за VIN."""

import json

from fastapi.testclient import TestClient

from tesla_ro.order import OrderStore
from tesla_ro.web import create_app

VIN = "5YJ3E1EA9LF000316"


def visit(session, name, odometer, op, date, recommendation=None):
    session.new_order()
    session.set_customer(name, "+380 67 123 45 67")
    session.set_vehicle(VIN, odometer_km=odometer)
    session.add_complaint("скарга")
    session.add_operation(op)
    if recommendation:
        session.add_recommendation(recommendation)
    session.order.created_at = date
    session.save_order()
    return session.order.number


def test_autosave_after_each_change(session):
    session.new_order()
    number = session.order.number
    assert session.store.list() == []  # порожня чернетка не зберігається
    session.set_customer("Іваненко Іван", "+380670000000")
    assert session.store.load(number).customer.name == "Іваненко Іван"
    session.add_operation("DIAG-01")
    saved = session.store.load(number)
    assert saved.status == "чернетка" and [l.code for l in saved.labor] == ["DIAG-01"]
    session.remove_line("labor", 1)
    assert session.store.load(number).labor == []
    assert not list(session.store.dir.glob("*.tmp"))


def test_autosave_failure_does_not_break_tool(session, monkeypatch):
    session.new_order()
    def broken(order):
        raise OSError("disk full")
    monkeypatch.setattr(session.store, "save", broken)
    session.set_customer("Іваненко Іван")  # не падає
    assert session.order.customer.name == "Іваненко Іван"


def test_failed_tool_is_not_saved(session):
    session.new_order()
    session.set_customer("Іваненко Іван")
    try:
        session.add_operation("NO-SUCH")
    except Exception:
        pass
    assert session.store.load(session.order.number).labor == []


def test_vehicle_history(session):
    first = visit(session, "Іваненко Іван", 30000, "BRK-PAD-F", "10.01.2026 10:00", "Замінити гальмівну рідину")
    second = visit(session, "Іваненко Іван", 45000, "TIRE-ROT", "15.06.2026 12:30")
    session.new_order()
    result = json.loads(session.set_vehicle(VIN.lower(), odometer_km=60000))
    assert result["previous_visits_count"] == 2
    assert [v["number"] for v in result["previous_visits"]] == [second, first]  # спершу найновіший
    last = result["previous_visits"][0]
    assert last["odometer_km"] == 45000 and last["customer"]["name"] == "Іваненко Іван"
    assert last["works"] == ["Ротація коліс із балансуванням"]
    assert result["previous_visits"][1]["recommendations"] == ["Замінити гальмівну рідину"]

    full = json.loads(session.get_vehicle_history())
    assert [v["number"] for v in full] == [second, first]
    assert [h["number"] for h in session.snapshot()["history"]] == [second, first]  # поточний не входить

    session.new_order()
    assert json.loads(session.set_vehicle("7SAYGDEEXPF000001"))["previous_visits"] == []


def test_history_needs_vin(session):
    import pytest
    from anthropic.lib.tools import ToolError
    session.new_order()
    with pytest.raises(ToolError):
        session.get_vehicle_history()


def test_saved_orders_search(tmp_path, session):
    visit(session, "Іваненко Іван", 30000, "BRK-PAD-F", "10.01.2026 10:00")
    session.new_order()
    session.set_customer("Петренко Петро", "+380 50 999 88 77")
    session.set_vehicle("7SAYGDEEXPF000001", plate="АА7777КХ")
    web = TestClient(create_app(orders_dir=session.store.dir))

    def found(q):
        return [o["customer"] for o in web.get("/api/orders", params={"q": q}).json()]

    assert sorted(found("")) == ["Іваненко Іван", "Петренко Петро"]
    assert found("5yj3e1ea9") == ["Іваненко Іван"]
    assert found("999 88") == ["Петренко Петро"]
    assert found("0509998877") == ["Петренко Петро"]
    assert found("петренко АА7777") == ["Петренко Петро"]
    assert found("нікого") == []


def test_web_snapshot_includes_history(tmp_path, session):
    visit(session, "Іваненко Іван", 30000, "BRK-PAD-F", "10.01.2026 10:00")
    web = TestClient(create_app(orders_dir=session.store.dir))
    store = OrderStore(session.store.dir)
    number = store.list()[0].number
    opened = web.post(f"/api/orders/{number}/open").json()["order"]
    assert opened["history"] == []  # інших візитів немає
