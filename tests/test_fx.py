"""Гривня поруч із доларом: курс НБУ, свій курс, оплата гривнею."""

from decimal import Decimal

from fastapi.testclient import TestClient

from tesla_ro import fx
from tesla_ro.web import create_app

from test_send_client import saved

RATE = (Decimal("41.5"), "31.10.2026")


def test_rates_modes_cache_and_offline(tmp_path):
    calls = []

    def fetch():
        calls.append(1)
        return RATE

    rates = fx.Rates(tmp_path / "fx.json", fetch=fetch)
    r = rates.current({"mode": "nbu"})
    assert r["rate"] == Decimal("41.5") and r["date"] == "31.10.2026" and r["source"] == "курс НБУ"
    rates.current({"mode": "nbu"})
    assert len(calls) == 1  # сьогодні вже отримано — банк більше не питаємо
    marked = rates.current({"mode": "nbu", "markup": 1})
    assert marked["rate"] == Decimal("41.9150") and "+ 1%" in marked["source"]
    assert rates.current({"mode": "manual", "manual_rate": 42})["rate"] == Decimal("42")
    assert rates.current({"mode": "manual", "manual_rate": 0}) is None
    assert rates.current({"mode": "off"}) is None

    def down():
        raise OSError("немає мережі")

    # після перезапуску й без мережі — останній відомий курс із файлу
    offline = fx.Rates(tmp_path / "fx.json", fetch=down)
    assert offline.current({"mode": "nbu"})["rate"] == Decimal("41.5")
    assert fx.Rates(tmp_path / "none.json", fetch=down).current({"mode": "nbu"}) is None
    assert fx.uah("222", Decimal("41.5")) == Decimal("9213.00") and fx.to_usd("4150", Decimal("41.5")) == Decimal("100.00")


def test_uah_in_order_payment_print_and_settings(tmp_path, session):
    number = saved(session)  # 222 $
    web = TestClient(create_app(orders_dir=session.store.dir, fx_path=tmp_path / "fx.json", fx_fetch=lambda: RATE))
    web.post(f"/api/orders/{number}/open")
    state = web.get("/api/state").json()
    assert state["fx"] == {"rate": "41.5", "date": "31.10.2026", "source": "курс НБУ"}
    assert state["order"]["uah"]["amount"] == "9213.00" and state["order"]["uah"]["balance"] == "9213.00"

    # оплата гривнею перераховується в $ за курсом і зберігає суму в грн
    order = web.post("/api/order/payments", json={"amount": "4 150", "currency": "UAH", "method": "готівка"}).json()["order"]
    p = order["payments"][-1]
    assert p["amount"] == "100.00" and p["amount_uah"] == "4150.00" and p["rate"] == "41.5"
    assert order["balance"] == "122.00" and order["uah"]["balance"] == "5063.00"
    assert web.get("/api/cash").json()["received_uah"] == "4150.00"

    html = web.get(f"/print/{number}").text
    assert "До сплати у гривні" in html and "5 063,00 грн" in html and "курс НБУ 41,50 грн/$ на 31.10.2026" in html

    # налаштування: свій курс і вимкнення
    base = {"name": "DK-TeslaService", "vat_percent": 0}
    assert web.put("/api/catalog/settings", json={**base, "uah_mode": "manual", "uah_rate": ""}).status_code == 400
    web.put("/api/catalog/settings", json={**base, "uah_mode": "manual", "uah_rate": "42"})
    assert web.get("/api/state").json()["order"]["uah"]["amount"] == "9324.00"
    web.put("/api/catalog/settings", json={**base, "uah_mode": "off"})
    assert web.get("/api/state").json()["order"]["uah"] is None
    assert "у гривні" not in web.get(f"/print/{number}").text
    r = web.post("/api/order/payments", json={"amount": "100", "currency": "UAH", "method": "готівка"})
    assert r.status_code == 400 and "Курс гривні недоступний" in r.json()["error"]
