"""Склад запчастин."""

import io
import json

import httpx
import openpyxl
import pytest
from fastapi.testclient import TestClient

from tesla_ro.stock import Stock, StockError
from tesla_ro.telegram import TelegramAPI, TelegramBot
from tesla_ro.web import create_app

from test_send_client import GROUP, FakeTelegram, saved

PAD = "PAD-F"  # колодки, які додає BRK-PAD-F у saved()


def test_receive_adjust_min_and_moves(tmp_path):
    stock = Stock(tmp_path / "stock.json")
    stock.receive(PAD, "5", by="Іван", note="накладна 12")
    stock.receive(PAD, "0,5")
    assert stock.qty(PAD) == 5.5
    with pytest.raises(StockError):
        stock.receive(PAD, "0")
    with pytest.raises(StockError):
        stock.receive(PAD, "-1")
    assert stock.adjust(PAD, "4")["qty"] == -1.5
    assert stock.adjust(PAD, "4") is None  # без змін — без запису
    stock.set_min(PAD, 4)
    assert stock.is_low(PAD)
    moves = stock.moves(PAD)
    assert [m["kind"] for m in moves] == ["інвентаризація", "прихід", "прихід"] and moves[-1]["note"] == "накладна 12"
    assert Stock(tmp_path / "stock.json").qty(PAD) == 4  # переживає перезапуск


def test_write_off_follows_order_status(tmp_path, session):
    stock = Stock(tmp_path / "stock.json")
    stock.receive(PAD, 3)
    parts = session.catalog.parts
    saved(session)
    order = session.order
    session.add_custom_part("Наліпка", 1, 2)  # власна позиція — на склад не впливає
    assert stock.sync_order(order, parts) == [] and stock.qty(PAD) == 3  # ще не видано
    order.set_status("видано")
    stock.sync_order(order, parts)
    stock.sync_order(order, parts)  # повторне збереження не списує вдруге
    assert stock.qty(PAD) == 2
    order.parts[0].qty = 2  # виправили кількість у вже виданому
    stock.sync_order(order, parts)
    assert stock.qty(PAD) == 1
    order.set_status("готово")  # повернули статус — запчастини повертаються на склад
    stock.sync_order(order, parts)
    assert stock.qty(PAD) == 3
    kinds = [m["kind"] for m in stock.moves(PAD)]
    assert kinds == ["повернення", "списання", "списання", "прихід"]

    # мінімум: сповіщення лише в момент, коли залишок опустився до нього
    stock.set_min(PAD, 1)
    order.set_status("видано")
    assert stock.sync_order(order, parts) == [PAD] and stock.qty(PAD) == 1


def test_orders_issued_before_stock_started_are_not_written_off(tmp_path, session):
    saved(session)
    order = session.order
    order.set_status("видано")
    order.status_log[-1]["at"] = "01.01.2025 10:00"
    stock = Stock(tmp_path / "stock.json")
    stock.receive(PAD, 3)
    assert stock.sync_order(order, session.catalog.parts) == [] and stock.qty(PAD) == 3


def test_stock_api_reserved_alert_and_assistant(tmp_path, session):
    number = saved(session)
    fake = FakeTelegram()
    api = TelegramAPI("TOKEN", httpx.Client(transport=httpx.MockTransport(fake.handler)))
    app = create_app(orders_dir=session.store.dir, stock_path=tmp_path / "stock.json",
                     telegram=lambda store, comments: TelegramBot(api, GROUP, store, comments, tmp_path / "tg.json"))
    web, bot = TestClient(app), app.state.telegram

    item = web.post(f"/api/stock/{PAD}/receive", json={"qty": "2", "note": "постачальник А", "by": "Іван"}).json()["item"]
    assert item["qty"] == 2 and item["reserved"] == 1 and item["free"] == 1  # замовлення прийнято — колодки в резерві
    assert web.post(f"/api/stock/{PAD}/receive", json={"qty": "abc"}).status_code == 400
    assert web.post("/api/stock/NOPE/receive", json={"qty": "1"}).status_code == 404
    web.put(f"/api/stock/{PAD}/min", json={"qty": "1"})
    row = next(i for i in web.get("/api/stock").json()["items"] if i["part_no"] == PAD)
    assert row["min"] == 1 and not row["low"]

    # у панелі замовлення видно залишок
    web.post(f"/api/orders/{number}/open")
    assert web.get("/api/state").json()["order"]["stock"] == {PAD: 2}

    # видали — списано, залишок дійшов до мінімуму, бот пише в групу
    web.post("/api/order/status", json={"status": "видано"})
    bot.flush()
    assert next(i for i in web.get("/api/stock").json()["items"] if i["part_no"] == PAD)["qty"] == 1
    alert = [m for m in fake.sent("sendMessage") if "Закінчується на складі" in m["text"]]
    assert len(alert) == 1 and "залишилось <b>1 компл</b>" in alert[0]["text"]
    assert "📦 Треба замовити (1)" in bot.daily_summary()
    moves = web.get(f"/api/stock/{PAD}/moves").json()
    assert moves[0]["kind"] == "списання" and moves[0]["order"] == number

    xlsx = openpyxl.load_workbook(io.BytesIO(web.get("/api/stock/to-order.xlsx").content))["Замовити"]
    assert xlsx["A2"].value == PAD and xlsx["F2"].value >= 1

    # помічник бачить залишок під час пошуку запчастин
    session.stock = app.state.telegram.stock
    found = json.loads(session.search_parts("колодки"))
    assert any(p["part_no"] == PAD and p["in_stock"] == 1 for p in found)
