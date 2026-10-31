"""Закупівлі: замовлення постачальнику, приймання з ціною, собівартість і маржа."""

import io

import openpyxl
from fastapi.testclient import TestClient

from tesla_ro.stock import Stock
from tesla_ro.web import create_app

from test_send_client import saved

PAD = "PAD-F"  # колодки: ціна продажу 150 $


def test_average_cost(tmp_path):
    stock = Stock(tmp_path / "stock.json")
    stock.receive(PAD, 2, cost=100)
    stock.receive(PAD, 2, cost=80)
    assert stock.cost(PAD) == 90 and stock.qty(PAD) == 4
    stock.receive(PAD, 1)  # без ціни — собівартість не змінюється
    assert stock.cost(PAD) == 90
    assert stock.moves(PAD)[1]["cost"] == 80.0


def test_purchase_flow_and_margin(tmp_path, session):
    number = saved(session)  # у замовленні колодки × 1 за 150 $
    web = TestClient(create_app(orders_dir=session.store.dir, stock_path=tmp_path / "stock.json",
                                purchases_path=tmp_path / "purchases.json"))
    assert web.post("/api/purchases", json={"lines": []}).status_code == 400
    assert web.post("/api/purchases", json={"lines": [{"part_no": "NOPE", "qty": 1}]}).status_code == 400
    r = web.post("/api/purchases", json={"supplier": "Авто-Плюс", "lines": [
        {"part_no": "pad-f", "qty": "3", "cost": "95,5"}, {"part_no": PAD, "qty": 1, "cost": 95.5},
        {"part_no": "CABIN-FLT-3Y", "qty": 2, "cost": 20}]}).json()
    po = r["purchase"]
    assert po["number"].startswith("ЗП-") and po["status"] == "чернетка" and r["suppliers"] == ["Авто-Плюс"]
    assert [x["qty"] for x in po["lines"]] == [4, 2] and po["total"] == "422.00"  # однаковий артикул зведено
    xlsx = openpyxl.load_workbook(io.BytesIO(web.get(f"/api/purchases/{po['id']}/export.xlsx").content)).active
    assert po["number"] in xlsx["A1"].value and any(row[0].value == PAD for row in xlsx.iter_rows())

    assert web.put(f"/api/purchases/{po['id']}", json={"status": "отримано"}).status_code == 400
    po = web.put(f"/api/purchases/{po['id']}", json={"status": "замовлено"}).json()["purchase"]
    assert po["status"] == "замовлено" and po["ordered_at"]

    # прийняли частково, ціну уточнили під час приймання
    po = web.post(f"/api/purchases/{po['id']}/receive", json={"lines": [{"part_no": PAD, "qty": 2, "cost": 90}]}).json()["purchase"]
    assert po["status"] == "отримано частково"
    item = next(i for i in web.get("/api/stock").json()["items"] if i["part_no"] == PAD)
    assert item["qty"] == 2 and item["cost"] == 90 and item["markup"] == 67  # 150 / 90
    assert web.get(f"/api/stock/{PAD}/moves").json()[0]["note"] == f"{po['number']}, Авто-Плюс"
    assert web.post(f"/api/purchases/{po['id']}/receive", json={"lines": [{"part_no": PAD, "qty": 0}]}).status_code == 400
    po = web.post(f"/api/purchases/{po['id']}/receive", json={"lines": [
        {"part_no": PAD, "qty": 2}, {"part_no": "CABIN-FLT-3Y", "qty": 2}]}).json()["purchase"]
    assert po["status"] == "отримано"
    assert web.put(f"/api/purchases/{po['id']}", json={"lines": []}).status_code == 400  # отримане не змінюють

    # маржа на запчастинах у замовленні: 150 − 90
    web.post(f"/api/orders/{number}/open")
    margin = web.get("/api/state").json()["order"]["margin"]
    assert margin == {"amount": "60.00", "percent": 40, "unknown": 0}
    session.add_custom_part("Наліпка", 1, 5)  # без собівартості
    session.store.save(session.order)
    assert web.get(f"/api/orders/{number}/extras").json()["margin"]["unknown"] == 1


def test_manual_receipt_with_cost(tmp_path, session):
    web = TestClient(create_app(orders_dir=session.store.dir, stock_path=tmp_path / "stock.json"))
    item = web.post(f"/api/stock/{PAD}/receive", json={"qty": "2", "cost": "100"}).json()["item"]
    assert item["cost"] == 100 and item["markup"] == 50
    assert web.post(f"/api/stock/{PAD}/receive", json={"qty": "1", "cost": "abc"}).status_code == 400
