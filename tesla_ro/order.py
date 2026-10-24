"""Модель заказ-наряда и хранилище."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path


def money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class Customer:
    name: str = ""
    phone: str = ""
    email: str = ""


@dataclass
class Vehicle:
    vin: str = ""
    model: str = ""
    model_code: str = ""
    year: int | None = None
    color: str = ""
    plate: str = ""
    odometer_km: int | None = None
    battery_percent: int | None = None


@dataclass
class LaborLine:
    code: str
    name: str
    hours: float
    rate: float
    discount_percent: float = 0

    @property
    def total(self) -> Decimal:
        return money(Decimal(str(self.hours)) * Decimal(str(self.rate)) * (1 - Decimal(str(self.discount_percent)) / 100))


@dataclass
class PartLine:
    part_no: str
    name: str
    qty: float
    price: float
    unit: str = "шт"
    discount_percent: float = 0

    @property
    def total(self) -> Decimal:
        return money(Decimal(str(self.qty)) * Decimal(str(self.price)) * (1 - Decimal(str(self.discount_percent)) / 100))


@dataclass
class RepairOrder:
    number: str
    created_at: str = field(default_factory=lambda: datetime.now().strftime("%d.%m.%Y %H:%M"))
    status: str = "черновик"
    customer: Customer = field(default_factory=Customer)
    vehicle: Vehicle = field(default_factory=Vehicle)
    complaints: list[str] = field(default_factory=list)
    diagnosis: str = ""
    recommendations: list[str] = field(default_factory=list)
    labor: list[LaborLine] = field(default_factory=list)
    parts: list[PartLine] = field(default_factory=list)
    currency: str = "₽"
    vat_percent: float = 0
    prices_include_vat: bool = True

    def totals(self) -> dict[str, Decimal]:
        labor = sum((line.total for line in self.labor), Decimal("0"))
        parts = sum((line.total for line in self.parts), Decimal("0"))
        subtotal = labor + parts
        rate = Decimal(str(self.vat_percent)) / 100
        if self.prices_include_vat:
            vat = money(subtotal * rate / (1 + rate))
            grand = subtotal
        else:
            vat = money(subtotal * rate)
            grand = subtotal + vat
        return {"labor": money(labor), "parts": money(parts), "vat": vat, "total": money(grand)}

    def missing_fields(self) -> list[str]:
        missing = []
        if not self.customer.name:
            missing.append("ФИО клиента")
        if not self.customer.phone:
            missing.append("телефон клиента")
        if not self.vehicle.vin:
            missing.append("VIN")
        if self.vehicle.odometer_km is None:
            missing.append("пробег")
        if not self.complaints:
            missing.append("жалобы клиента")
        if not self.labor:
            missing.append("работы")
        return missing

    def to_dict(self) -> dict:
        data = asdict(self)
        data["totals"] = {k: str(v) for k, v in self.totals().items()}
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "RepairOrder":
        data = dict(data)
        data.pop("totals", None)
        data["customer"] = Customer(**data.get("customer", {}))
        data["vehicle"] = Vehicle(**data.get("vehicle", {}))
        data["labor"] = [LaborLine(**x) for x in data.get("labor", [])]
        data["parts"] = [PartLine(**x) for x in data.get("parts", [])]
        return cls(**data)


NUMBER_RE = re.compile(r"^ЗН-\d{4}-\d{5}$")


class OrderStore:
    """Хранит заказ-наряды в виде JSON-файлов в каталоге."""

    def __init__(self, directory: str | Path = "orders"):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._reserved: set[str] = set()

    def next_number(self) -> str:
        """Выдать новый номер. Номер резервируется, даже если заказ ещё не сохранён,
        чтобы параллельные сеансы не получили одинаковые номера."""
        year = datetime.now().year
        prefix = f"ЗН-{year}-"
        with self._lock:
            taken = {p.stem for p in self.dir.glob(f"{prefix}*.json")} | self._reserved
            nums = [int(n[len(prefix):]) for n in taken if n.startswith(prefix) and n[len(prefix):].isdigit()]
            number = f"{prefix}{max(nums, default=0) + 1:05d}"
            self._reserved.add(number)
            return number

    def _path(self, number: str) -> Path:
        if not NUMBER_RE.match(number):
            raise FileNotFoundError(number)
        return self.dir / f"{number}.json"

    def list(self) -> list[RepairOrder]:
        orders = []
        for path in sorted(self.dir.glob("ЗН-*.json"), reverse=True):
            try:
                orders.append(RepairOrder.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (ValueError, TypeError, KeyError):
                continue
        return orders

    def save(self, order: RepairOrder) -> Path:
        path = self._path(order.number)
        path.write_text(json.dumps(order.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def load(self, number: str) -> RepairOrder:
        return RepairOrder.from_dict(json.loads(self._path(number).read_text(encoding="utf-8")))
