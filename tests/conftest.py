import pytest

from tesla_ro.catalog import Catalog
from tesla_ro.order import OrderStore
from tesla_ro.tools import OrderSession


@pytest.fixture
def session(tmp_path):
    return OrderSession(Catalog.load(), OrderStore(tmp_path))


@pytest.fixture(autouse=True)
def nbu_offline(monkeypatch):
    """Тести не ходять у мережу: «банк недоступний», якщо тест не підставив свій курс."""
    from tesla_ro import fx

    def offline():
        raise OSError("NBU недоступний у тестах")
    monkeypatch.setattr(fx.Rates, "_fetch_nbu", staticmethod(offline))
