"""Редагування довідника: перевірка даних, збереження на диск, API."""

import json

import pytest
from fastapi.testclient import TestClient

from tesla_ro.catalog import DEFAULT_CATALOG, Catalog, CatalogError
from tesla_ro.web import create_app

OP = {"code": "brk-test", "name": "Тестова робота", "category": "Гальмівна система", "hours": "1,5",
      "models": ["MODEL_3", "MODEL_Y"], "parts": ["pad-f"], "interval": ""}


@pytest.fixture
def catalog(tmp_path):
    return Catalog.load_editable(tmp_path / "catalog.json")


def test_first_start_copies_default(tmp_path):
    path = tmp_path / "data" / "catalog.json"
    cat = Catalog.load_editable(path)
    assert path.exists() and len(cat.operations) == len(Catalog.load().operations)


def test_operation_crud_persists(catalog):
    op = catalog.save_operation(OP, create=True)
    assert (op.code, op.hours, op.parts) == ("BRK-TEST", 1.5, ["PAD-F"])
    with pytest.raises(CatalogError, match="уже є"):
        catalog.save_operation(OP, create=True)
    catalog.save_operation({**OP, "code": "BRK-TEST", "hours": 2, "models": ["*", "MODEL_S"]}, create=False)
    reloaded = Catalog.load(catalog.path)
    assert reloaded.operations["BRK-TEST"].hours == 2 and reloaded.operations["BRK-TEST"].models == ["*"]
    catalog.delete_operation("BRK-TEST")
    assert "BRK-TEST" not in Catalog.load(catalog.path).operations
    with pytest.raises(KeyError):
        catalog.delete_operation("BRK-TEST")


@pytest.mark.parametrize("bad, message", [
    ({"code": "з пробілом"}, "латинські"),
    ({"name": "  "}, "Назва"),
    ({"hours": "0"}, "Нормогодини"),
    ({"hours": "abc"}, "числом"),
    ({"models": []}, "модель"),
    ({"models": ["MODEL_Z"]}, "Невідома модель"),
    ({"parts": ["NOPE"]}, "NOPE"),
    ({"category": ""}, "Розділ"),
])
def test_operation_validation(catalog, bad, message):
    with pytest.raises(CatalogError, match=message):
        catalog.save_operation({**OP, **bad}, create=True)


def test_part_in_use_cannot_be_deleted(catalog):
    with pytest.raises(CatalogError, match="BRK-PAD-F"):
        catalog.delete_part("PAD-F")
    catalog.save_part({"part_no": "new-1", "name": "Нова", "price": "12,5"}, create=True)
    assert Catalog.load(catalog.path).parts["NEW-1"].price == 12.5
    catalog.delete_part("NEW-1")
    assert "NEW-1" not in catalog.parts


def test_failed_write_rolls_back(catalog, monkeypatch):
    def broken():
        raise OSError("disk full")
    monkeypatch.setattr(catalog, "save", broken)
    with pytest.raises(OSError):
        catalog.save_operation(OP, create=True)
    assert "BRK-TEST" not in catalog.operations


def test_settings(catalog):
    catalog.update_settings({"name": "DK", "address": "Одеса", "labor_rate_per_hour": "75",
                             "vat_percent": 0})
    reloaded = Catalog.load(catalog.path)
    assert reloaded.service["name"] == "DK" and reloaded.labor_rate == 75
    with pytest.raises(CatalogError):
        catalog.update_settings({"name": "", "labor_rate_per_hour": 1, "vat_percent": 0})


def test_default_catalog_untouched_without_path(tmp_path):
    before = DEFAULT_CATALOG.read_text(encoding="utf-8")
    web = TestClient(create_app(orders_dir=tmp_path))
    assert web.post("/api/catalog/operations", json=OP).status_code == 200
    assert DEFAULT_CATALOG.read_text(encoding="utf-8") == before


def test_api_edit_flow(tmp_path):
    path = tmp_path / "catalog.json"
    web = TestClient(create_app(path, tmp_path / "orders", password="secret-pass"))
    assert web.post("/api/catalog/operations", json=OP).status_code == 401  # без входу не можна

    web.post("/login", data={"password": "secret-pass"})
    data = web.post("/api/catalog/operations", json=OP).json()
    op = next(o for o in data["operations"] if o["code"] == "BRK-TEST")
    assert op["labor_price"] == 90 and op["models"] == "Model 3, Model Y"

    r = web.post("/api/catalog/operations", json=OP)
    assert r.status_code == 400 and "уже є" in r.json()["error"]
    assert web.put("/api/catalog/operations/NOPE", json=OP).status_code == 404

    web.put("/api/catalog/settings", json={"name": "DK-TeslaService", "address": "м. Одеса",
                                            "labor_rate_per_hour": 80, "vat_percent": 0})
    data = web.get("/api/catalog").json()
    assert next(o for o in data["operations"] if o["code"] == "BRK-TEST")["labor_price"] == 120
    assert data["settings"]["labor_rate_per_hour"] == 80

    r = web.delete("/api/catalog/parts/PAD-F")
    assert r.status_code == 400 and "використовується" in r.json()["error"]
    assert next(p for p in data["parts"] if p["part_no"] == "PAD-F")["used"] >= 3

    # нова сесія сервера читає збережений файл
    again = TestClient(create_app(path, tmp_path / "orders"))
    assert any(o["code"] == "BRK-TEST" for o in again.get("/api/catalog").json()["operations"])
    assert json.loads(path.read_text(encoding="utf-8"))["labor_rate_per_hour"] == 80


def test_new_orders_use_edited_rate(tmp_path):
    from tesla_ro.order import OrderStore
    from tesla_ro.tools import OrderSession

    cat = Catalog.load_editable(tmp_path / "catalog.json")
    cat.update_settings({"name": "DK", "labor_rate_per_hour": 100, "vat_percent": 0})
    session = OrderSession(cat, OrderStore(tmp_path / "orders"))
    session.new_order()
    session.add_operation("DIAG-01")
    assert str(session.order.totals()["labor"]) == "100.00"
