"""Оплата, каса за день і боржники."""

from datetime import date
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from tesla_ro.order import PaymentError, RepairOrder
from tesla_ro.render import render_html
from tesla_ro.telegram import TelegramAPI, TelegramBot
from tesla_ro.web import create_app

from test_send_client import GROUP, FakeTelegram, saved


def test_payment_model(session):
    saved(session)  # разом 222,00
    o = session.order
    assert o.payment_status() == "не оплачено" and o.balance() == Decimal("222.00")
    o.add_payment("100,50", "картка", by="Іван")
    assert o.paid() == Decimal("100.50") and o.balance() == Decimal("121.50") and o.payment_status() == "частково"
    o.add_payment("121.50", "готівка")
    assert o.payment_status() == "оплачено"
    o.add_payment("10", "переказ")
    assert o.payment_status() == "переплата" and o.balance() == Decimal("-10.00")
    refund = o.add_payment("-10", "переказ", note="повернення")
    assert o.payment_status() == "оплачено"
    o.remove_payment(refund["id"])
    assert o.balance() == Decimal("-10.00")
    for bad in ("", "0", "abc", "1e12"):
        with pytest.raises(PaymentError):
            o.add_payment(bad, "готівка")
    with pytest.raises(PaymentError):
        o.add_payment("5", "біткоїн")
    data = o.to_dict()
    assert data["paid"] == "232.00" and data["balance"] == "-10.00" and data["payment_status"] == "переплата"
    assert RepairOrder.from_dict(data).paid() == Decimal("232.00")

    html = render_html(o)
    assert "Сплачено:" in html and "Переплата:" in html


def test_payments_api_cash_and_debts(session):
    number = saved(session)
    web = TestClient(create_app(orders_dir=session.store.dir))
    web.post(f"/api/orders/{number}/open")
    r = web.post("/api/order/payments", json={"amount": "50", "method": "готівка", "by": "Іван", "note": "передоплата"})
    order = r.json()["order"]
    assert order["paid"] == "50.00" and order["balance"] == "172.00" and order["payment_status"] == "частково"
    assert web.post("/api/order/payments", json={"amount": "x", "method": "готівка"}).status_code == 400
    card = web.post("/api/order/payments", json={"amount": "100", "method": "картка"}).json()["order"]["payments"][-1]
    assert session.store.load(number).paid() == Decimal("150.00")  # збережено одразу

    cash = web.get("/api/cash").json()
    assert cash["day"] == date.today().isoformat() and cash["total"] == "150.00"
    assert cash["by_method"] == {"готівка": "50.00", "картка": "100.00", "переказ": "0.00"}
    assert cash["payments"][0]["number"] == number and cash["payments"][0]["client"] == "Петренко Петро"
    assert web.get("/api/cash?day=2020-01-01").json()["payments"] == []

    # видано з боргом — у боржниках
    assert web.get("/api/debts").json()["debts"] == []
    web.post("/api/order/status", json={"status": "видано"})
    debts = web.get("/api/debts").json()
    assert debts["total"] == "72.00" and debts["debts"][0]["number"] == number
    assert web.get("/api/orders").json()[0]["balance"] == "72.00"

    # оплату, внесену помилково, можна видалити; скасоване з оплатою — «треба повернути»
    assert web.delete(f"/api/order/payments?payment_id={card['id']}").json()["removed"]["amount"] == "100.00"
    assert web.delete(f"/api/order/payments?payment_id={card['id']}").status_code == 400
    web.post("/api/order/status", json={"status": "скасовано"})
    d = web.get("/api/debts").json()
    assert d["debts"] == [] and d["overpaid"][0]["balance"] == "-50.00"


def test_telegram_summary_and_debt_warning(tmp_path, session):
    number = saved(session)
    fake = FakeTelegram()
    api = TelegramAPI("TOKEN", httpx.Client(transport=httpx.MockTransport(fake.handler)))
    app = create_app(orders_dir=session.store.dir,
                     telegram=lambda store, comments: TelegramBot(api, GROUP, store, comments, tmp_path / "tg.json"))
    web, bot = TestClient(app), app.state.telegram
    bot.order_saved(session.store.load(number))
    bot.flush()
    web.post(f"/api/orders/{number}/open")
    web.post("/api/order/payments", json={"amount": "22", "method": "картка"})
    web.post("/api/order/status", json={"status": "видано"})
    bot.flush()
    assert any("видано" in m["text"] and "борг 200,00" in m["text"] for m in fake.sent("sendMessage"))
    text = bot.daily_summary()
    assert "💵 Отримано: <b>22,00 $</b> (картка 22,00)" in text
    assert "Борги клієнтів: 200,00 $" in text and number in text
