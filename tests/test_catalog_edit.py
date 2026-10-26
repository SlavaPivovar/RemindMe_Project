"""Редагування довідника: перевірка даних, збереження на диск, API."""

import json

import pytest
from fastapi.testclient import TestClient

from tesla_ro.catalog import DEFAULT_CATALOG, Catalog, CatalogError
from tesla_ro.web import create_app

OP = {"code": "brk-test", "name": "Тестова робота", "category": "Гальмівна система", "price": "95,5",
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
    assert (op.code, op.price, op.parts) == ("BRK-TEST", 95.5, ["PAD-F"])
    with pytest.raises(CatalogError, match="уже є"):
        catalog.save_operation(OP, create=True)
    catalog.save_operation({**OP, "code": "BRK-TEST", "price": 120, "models": ["*", "MODEL_S"]}, create=False)
    reloaded = Catalog.load(catalog.path)
    assert reloaded.operations["BRK-TEST"].price == 120 and reloaded.operations["BRK-TEST"].models == ["*"]
    catalog.delete_operation("BRK-TEST")
    assert "BRK-TEST" not in Catalog.load(catalog.path).operations
    with pytest.raises(KeyError):
        catalog.delete_operation("BRK-TEST")


@pytest.mark.parametrize("bad, message", [
    ({"code": "з пробілом"}, "латинські"),
    ({"name": "  "}, "Назва"),
    ({"price": "-1"}, "Ціна"),
    ({"price": "abc"}, "числом"),
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
    catalog.update_settings({"name": "DK", "address": "Одеса", "vat_percent": "7,5"})
    reloaded = Catalog.load(catalog.path)
    assert reloaded.service["name"] == "DK" and reloaded.vat_percent == 7.5
    with pytest.raises(CatalogError):
        catalog.update_settings({"name": "", "vat_percent": 0})


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
    assert op["price"] == 95.5 and op["models"] == "Model 3, Model Y"

    r = web.post("/api/catalog/operations", json=OP)
    assert r.status_code == 400 and "уже є" in r.json()["error"]
    assert web.put("/api/catalog/operations/NOPE", json=OP).status_code == 404

    web.put("/api/catalog/operations/BRK-TEST", json={**OP, "price": 110})
    web.put("/api/catalog/settings", json={"name": "DK-TeslaService", "address": "м. Одеса", "vat_percent": 0})
    data = web.get("/api/catalog").json()
    assert next(o for o in data["operations"] if o["code"] == "BRK-TEST")["price"] == 110
    assert data["settings"]["address"] == "м. Одеса" and "labor_rate_per_hour" not in data["settings"]

    r = web.delete("/api/catalog/parts/PAD-F")
    assert r.status_code == 400 and "використовується" in r.json()["error"]
    assert next(p for p in data["parts"] if p["part_no"] == "PAD-F")["used"] >= 3

    # нова сесія сервера читає збережений файл
    again = TestClient(create_app(path, tmp_path / "orders"))
    assert any(o["code"] == "BRK-TEST" for o in again.get("/api/catalog").json()["operations"])
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert next(o for o in saved["operations"] if o["code"] == "BRK-TEST")["price"] == 110


def test_new_orders_use_edited_price(tmp_path):
    from tesla_ro.order import OrderStore
    from tesla_ro.tools import OrderSession

    cat = Catalog.load_editable(tmp_path / "catalog.json")
    diag = cat.operations["DIAG-01"]
    cat.save_operation({**diag.__dict__, "price": 100}, create=False)
    session = OrderSession(cat, OrderStore(tmp_path / "orders"))
    session.new_order()
    session.add_operation("DIAG-01")
    assert str(session.order.totals()["labor"]) == "100.00"


def test_legacy_catalog_with_hours_is_converted(tmp_path):
    path = tmp_path / "catalog.json"
    legacy = {"service": {"name": "S"}, "currency": "$", "vat_percent": 0, "labor_rate_per_hour": 70,
              "operations": [{"code": "OLD-1", "name": "Стара робота", "hours": 1.5, "models": ["*"],
                              "parts": [], "category": "Діагностика", "interval": ""}],
              "parts": []}
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    cat = Catalog.load_editable(path)
    assert cat.operations["OLD-1"].price == 105
    saved = json.loads(path.read_text(encoding="utf-8"))  # файл одразу переписано в новому форматі
    assert "labor_rate_per_hour" not in saved and saved["operations"][0] == {
        "code": "OLD-1", "name": "Стара робота", "price": 105, "models": ["*"], "parts": [],
        "category": "Діагностика", "interval": ""}
