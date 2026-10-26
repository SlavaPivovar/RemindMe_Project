"""Статуси замовлення-наряду."""

import pytest

from tesla_ro.order import RepairOrder, StatusError


def test_lifecycle_and_log(session):
    session.new_order()
    order = session.order
    with pytest.raises(StatusError, match="не заповнено"):
        order.set_status("в роботі")
    order.set_status("скасовано")  # скасувати можна й неповне
    assert order.status == "скасовано"
    session.set_customer("Іваненко Іван", "+380670000000")
    session.set_vehicle("5YJ3E1EA9LF000316", odometer_km=1000)
    session.add_complaint("скарга")
    session.add_operation("DIAG-01")
    for status in ("прийнято", "в роботі", "готово", "видано"):
        order.set_status(status)
    order.set_status("видано")  # повтор не дублює запис
    assert [e["status"] for e in order.status_log] == ["скасовано", "прийнято", "в роботі", "готово", "видано"]
    assert all(e["at"] for e in order.status_log)
    with pytest.raises(StatusError, match="Невідомий"):
        order.set_status("оплачено частково")


def test_legacy_statuses_are_mapped():
    for old, new in (("відкрито", "прийнято"), ("виконано", "готово"), ("закрито", "видано")):
        assert RepairOrder.from_dict({"number": "x", "status": old}).status == new
    with pytest.raises(StatusError, match="«прийнято»"):  # старі назви від помічника теж розуміємо
        RepairOrder(number="x").set_status("Відкрито ")
