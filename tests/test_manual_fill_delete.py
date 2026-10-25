"""Ручне заповнення порожнього замовлення-наряду та видалення замовлень."""

from fastapi.testclient import TestClient

from tesla_ro.order import OrderStore
from tesla_ro.web import create_app


def test_fill_new_order_by_hand(tmp_path):
    web = TestClient(create_app(orders_dir=tmp_path))
    order = web.post("/api/order/new").json()["order"]
    number = order["number"]
    assert order["labor"] == [] and "ПІБ клієнта" in order["missing_fields"]
    assert OrderStore(tmp_path).list() == []  # порожнє ще не зберігається

    web.put("/api/order/customer", json={"name": "Іваненко Іван", "phone": "+380670000000"})
    r = web.put("/api/order/vehicle", json={"vin": "5yj3e1ea9lf000316", "odometer_km": 48000, "plate": "аа 1234 вв"}).json()
    assert r["warnings"] == [] and r["order"]["vehicle"]["model"] == "Model 3"
    assert r["order"]["vehicle"]["plate"] == "АА1234ВВ"
    bad_vin = web.put("/api/order/vehicle", json={"vin": "5YJ3E1EA1LF000316", "odometer_km": 48000}).json()
    assert any("Контрольна цифра" in w for w in bad_vin["warnings"])
    web.put("/api/order/vehicle", json={"vin": "5YJ3E1EA9LF000316", "odometer_km": 48000})
    assert web.put("/api/order/vehicle", json={"vin": "5YJ3E1EA9LF000316", "odometer_km": -5}).status_code == 400

    web.post("/api/order/complaints", json={"text": "Скрип спереду"})
    web.post("/api/order/complaints", json={"text": "Друга скарга"})
    order = web.delete("/api/order/complaints/2").json()["order"]
    assert order["complaints"] == ["Скрип спереду"]
    assert web.post("/api/order/complaints", json={"text": "  "}).status_code == 400
    assert web.delete("/api/order/complaints/7").status_code == 400
    web.put("/api/order/diagnosis", json={"text": "Знос колодок"})
    web.post("/api/order/recommendations", json={"text": "Замінити рідину"})
    web.post("/api/order/labor", json={"code": "BRK-PAD-F"})
    order = web.post("/api/order/status", json={"status": "прийнято"}).json()["order"]
    assert order["missing_fields"] == [] and order["status"] == "прийнято"

    saved = OrderStore(tmp_path).load(number)
    assert (saved.customer.name, saved.vehicle.odometer_km, saved.diagnosis) == ("Іваненко Іван", 48000, "Знос колодок")
    assert saved.recommendations == ["Замінити рідину"] and len(saved.labor) == 1


def test_delete_order(tmp_path, session):
    session.new_order()
    session.set_customer("Перший")
    first = session.order.number
    session.new_order()
    session.set_customer("Другий")
    second = session.order.number
    (tmp_path / f"{second}.html").write_text("print", encoding="utf-8")

    app = create_app(orders_dir=session.store.dir)
    me, other = TestClient(app), TestClient(app)
    me.post(f"/api/orders/{second}/open")
    other.post(f"/api/orders/{second}/open")

    assert me.delete(f"/api/orders/{second}").json() == {"deleted": second}
    assert [o["number"] for o in me.get("/api/orders").json()] == [first]
    assert (session.store.trash / f"{second}.json").exists() and not (tmp_path / f"{second}.html").exists()
    assert me.get("/api/state").json()["order"] is None
    assert other.get("/api/state").json()["order"] is None  # в інших теж закрито
    assert me.delete(f"/api/orders/{second}").status_code == 404
    assert me.delete("/api/orders/../../etc").status_code == 404

    # відкрите деінде замовлення не воскресне від автозбереження
    session.set_customer("Друге оновлене")  # session ще тримає видалене замовлення
    assert not (session.store.dir / f"{second}.json").exists()

    # номер видаленого не дістанеться новому замовленню, навіть після перезапуску
    fresh = OrderStore(session.store.dir)
    assert fresh.next_number() not in (first, second)
