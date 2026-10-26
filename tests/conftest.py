import pytest

from tesla_ro.catalog import Catalog
from tesla_ro.order import OrderStore
from tesla_ro.tools import OrderSession


@pytest.fixture
def session(tmp_path):
    return OrderSession(Catalog.load(), OrderStore(tmp_path))
