"""Головна сторінка: дошка замовлень, записи на сьогодні, «потребує уваги», швидка зміна статусу."""

from datetime import date

from fastapi.testclient import TestClient

from tesla_ro.web import create_app

from test_send_client import saved


def test_dashboard_board_kpi_and_alerts(tmp_path, session):
    a = saved(session)  # прийнято
    b = saved(session, phone="+380 50 000 00 02")
    session.save_order("в роботі")
    order = session.store.load(b)
    order.status_log[-1]["at"] = "01.10.2026 10:00"  # давно в роботі
    order.assignee = "Іван"
    session.store.save(order)
    c = saved(session, phone="+380 50 000 00 03")
    session.save_order("в роботі")
    session.save_order("видано")  # видано з боргом
    session.new_order()  # чернетка
    session.set_customer("Чернетка", "")
    web = TestClient(create_app(orders_dir=session.store.dir, bookings_path=tmp_path / "b.json"))
    web.post("/api/bookings", json={"date": date.today().isoformat(), "time": "00:01", "client_name": "Ранній"})
    web.post("/api/bookings", json={"date": date.today().isoformat(), "time": "23:59", "client_name": "Пізній"})

    d = web.get("/api/dashboard").json()
    assert [t["number"] for t in d["board"]["прийнято"]] == [a]
    tile = d["board"]["в роботі"][0]
    assert tile["number"] == b and tile["stale"] and tile["assignee"] == "Іван" and tile["days"] > 2
    assert tile["next"] == {"status": "готово", "label": "✅ Готово"} and tile["car"].startswith("Tesla Model")
    assert d["board"]["готово"] == []
    assert d["kpi"]["accepted"] == 1 and d["kpi"]["in_work"] == 1 and d["kpi"]["bookings_today"] == 2
    assert d["kpi"]["debts"] == "222.00" and [x["client"] for x in d["drafts"]] == ["Чернетка"]
    kinds = [x["kind"] for x in d["alerts"]]
    assert "stale" in kinds and "debt" in kinds and kinds.count("late") == 1  # 00:01 минув, 23:59 — ще ні
    assert any("борг 222,00 $" in x["text"] for x in d["alerts"])
    assert [x["client_name"] for x in d["bookings_today"]] == ["Ранній", "Пізній"]

    # швидка зміна статусу з головної — з відкритою на сайті копією замовлення
    web.post(f"/api/orders/{b}/open")
    r = web.post(f"/api/orders/{b}/status", json={"status": "готово", "by": "Олег"})
    assert r.status_code == 200 and r.json()["status"] == "готово"
    assert session.store.load(b).status_log[-1]["by"] == "Олег"
    web.post("/api/order/complaints", json={"text": "ще одна"})  # автозбереження відкритої копії
    assert session.store.load(b).status == "готово"
    assert web.post(f"/api/orders/{b}/status", json={"status": "щось"}).status_code == 400
    assert web.post("/api/orders/ЗН-2026-09999/status", json={"status": "готово"}).status_code == 404
    d = web.get("/api/dashboard").json()
    assert [t["number"] for t in d["board"]["готово"]] == [b] and c not in str(d["board"])
