import json
from decimal import Decimal

import pytest
from anthropic import beta_tool
from anthropic.lib.tools import ToolError

from tesla_ro.order import PartLine, RepairOrder


def fill(session):
    session.new_order()
    session.set_customer("Іваненко Іван", "+380 67 000 00 00")
    session.set_vehicle("5YJ3E1EA9LF000316", odometer_km=48000, plate="аа 1234 вв")
    session.add_complaint("Скрип під час гальмування")


def test_numbering(session):
    first = json.loads(session.new_order())["number"]
    second = json.loads(session.new_order())["number"]
    assert first.endswith("-00001") and second.endswith("-00002")  # номер резервируется сразу
    fill(session)  # замовлення -00003
    session.add_operation("BRK-PAD-F")
    session.save_order()
    assert json.loads(session.new_order())["number"].endswith("-00004")
    assert [o.number[-5:] for o in session.store.list()] == ["00003"]


def test_store_rejects_bad_number(session):
    with pytest.raises(ToolError):
        session.open_order("../../etc/passwd")


def test_vehicle_from_vin(session):
    fill(session)
    v = session.order.vehicle
    assert (v.model, v.year, v.plate) == ("Model 3", 2020, "АА1234ВВ")


def test_operation_adds_parts_and_totals(session):
    fill(session)
    session.add_operation("BRK-PAD-F")
    session.add_operation("BRK-FLUID")
    t = session.order.totals()
    # 2.2 н/год * $60 = 132; колодки 150 + рідина 15
    assert t["labor"] == Decimal("132.00")
    assert t["parts"] == Decimal("165.00")
    assert t["total"] == Decimal("297.00")
    # ПДВ 20% у складі суми
    assert t["vat"] == Decimal("49.50")


def test_vat_on_top():
    order = RepairOrder(number="x", vat_percent=20, prices_include_vat=False)
    order.parts.append(PartLine(part_no="p", name="p", qty=2, price=50))
    assert order.totals()["total"] == Decimal("120.00")


def test_model_filter(session):
    fill(session)  # Model 3
    codes = {o["code"] for o in json.loads(session.search_operations("фільтр салону"))}
    assert "HVAC-FILTER" in codes and "HVAC-FILTER-SX" not in codes
    with pytest.raises(ToolError):
        session.add_operation("SUSP-AIR-SX")


def test_same_part_merges(session):
    fill(session)
    session.add_part("WIPER-SET")
    session.add_part("WIPER-SET", 2)
    assert len(session.order.parts) == 1 and session.order.parts[0].qty == 3


def test_discount_and_remove(session):
    fill(session)
    session.add_operation("DIAG-01")
    session.add_operation("TIRE-ROT")
    session.set_discount("labor", 0, 10)
    assert session.order.totals()["labor"] == Decimal("108.00")
    session.remove_line("labor", 1)
    assert [l.code for l in session.order.labor] == ["TIRE-ROT"]
    with pytest.raises(ToolError):
        session.remove_line("labor", 5)


def test_save_requires_fields(session, tmp_path):
    session.new_order()
    with pytest.raises(ToolError, match="не заповнено"):
        session.save_order("відкрито")
    session.save_order("чернетка")
    fill(session)
    session.add_operation("BRK-PAD-F")
    out = json.loads(session.save_order())
    html = open(out["html"], encoding="utf-8").read()
    assert "ЗАМОВЛЕННЯ-НАРЯД" in html and "5YJ3E1EA9LF000316" in html and "Скрип під час гальмування" in html
    reopened = session.store.load(session.order.number)
    assert reopened.totals() == session.order.totals()
    assert reopened.status == "відкрито"


def test_html_escapes(session):
    fill(session)
    session.add_complaint("<script>alert(1)</script>")
    session.add_custom_labor("Робота", 1)
    out = json.loads(session.save_order())
    assert "<script>" not in open(out["html"], encoding="utf-8").read()


def test_requires_order(session):
    with pytest.raises(ToolError, match="new_order"):
        session.add_complaint("x")


def test_all_methods_are_valid_tools(session):
    tools = [beta_tool(m).to_dict() for m in session.tool_methods()]
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names))
    for t in tools:
        assert t["description"]
        for name, prop in t["input_schema"]["properties"].items():
            assert prop.get("description"), f"{t['name']}.{name} без опису"
