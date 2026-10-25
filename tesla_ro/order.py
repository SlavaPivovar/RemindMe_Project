"""Модель замовлення-наряду та сховище."""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path


def money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# Життєвий цикл замовлення-наряду.
DRAFT = "чернетка"
CANCELLED = "скасовано"
STATUSES = (DRAFT, "прийнято", "в роботі", "готово", "видано", CANCELLED)
# Статуси попередніх версій програми.
LEGACY_STATUSES = {"відкрито": "прийнято", "виконано": "готово", "закрито": "видано"}


class StatusError(ValueError):
    """Статус не можна встановити; текст повідомлення показується користувачу."""


def now_str() -> str:
    return datetime.now().strftime("%d.%m.%Y %H:%M")


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
    price: float
    qty: float = 1
    discount_percent: float = 0

    @property
    def total(self) -> Decimal:
        return money(Decimal(str(self.qty)) * Decimal(str(self.price)) * (1 - Decimal(str(self.discount_percent)) / 100))

    @classmethod
    def from_dict(cls, data: dict) -> "LaborLine":
        data = dict(data)
        if "hours" in data:
            # Старий формат (нормогодини × ставка) — переводимо у фіксовану ціну рядка.
            data["price"] = float(money(Decimal(str(data.pop("hours"))) * Decimal(str(data.pop("rate", 0)))))
        return cls(**data)


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
    created_at: str = field(default_factory=now_str)
    status: str = DRAFT
    customer: Customer = field(default_factory=Customer)
    vehicle: Vehicle = field(default_factory=Vehicle)
    complaints: list[str] = field(default_factory=list)
    diagnosis: str = ""
    recommendations: list[str] = field(default_factory=list)
    labor: list[LaborLine] = field(default_factory=list)
    parts: list[PartLine] = field(default_factory=list)
    currency: str = "$"
    vat_percent: float = 0
    prices_include_vat: bool = True
    status_log: list[dict] = field(default_factory=list)  # [{"status": ..., "at": "дд.мм.рррр гг:хх"}]

    def set_status(self, status: str) -> None:
        status = LEGACY_STATUSES.get(status.strip().lower(), status.strip().lower())
        if status not in STATUSES:
            raise StatusError(f"Невідомий статус «{status}». Можливі: {', '.join(STATUSES)}")
        missing = self.missing_fields()
        if missing and status not in (DRAFT, CANCELLED):
            raise StatusError(f"Не можна встановити статус «{status}»: не заповнено — {', '.join(missing)}")
        if status != self.status:
            self.status = status
            self.status_log.append({"status": status, "at": now_str()})

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
            missing.append("ПІБ клієнта")
        if not self.customer.phone:
            missing.append("телефон клієнта")
        if not self.vehicle.vin:
            missing.append("VIN")
        if self.vehicle.odometer_km is None:
            missing.append("пробіг")
        if not self.complaints:
            missing.append("скарги клієнта")
        if not self.labor:
            missing.append("роботи")
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
        data["labor"] = [LaborLine.from_dict(x) for x in data.get("labor", [])]
        data["parts"] = [PartLine(**x) for x in data.get("parts", [])]
        data["status"] = LEGACY_STATUSES.get(data.get("status", DRAFT), data.get("status", DRAFT))
        return cls(**data)


def _parse_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%d.%m.%Y %H:%M")
    except ValueError:
        return datetime.min


NUMBER_RE = re.compile(r"^ЗН-\d{4}-\d{5}$")


class OrderStore:
    """Зберігає замовлення-наряди як JSON-файли в каталозі."""

    def __init__(self, directory: str | Path = "orders"):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._reserved: set[str] = set()

    def next_number(self) -> str:
        """Видати новий номер. Номер резервується, навіть якщо замовлення ще не збережене,
        щоб паралельні сеанси не отримали однакові номери."""
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
        # Пишемо в тимчасовий файл і атомарно підміняємо: при збої не лишиться напівзаписаного файлу.
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(order.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def history(self, vin: str, exclude: str | None = None) -> list[RepairOrder]:
        """Попередні замовлення-наряди автомобіля з цим VIN, від найновішого."""
        vin = vin.strip().upper().replace(" ", "")
        if not vin:
            return []
        found = [o for o in self.list() if o.vehicle.vin == vin and o.number != exclude]
        return sorted(found, key=lambda o: _parse_date(o.created_at), reverse=True)

    def load(self, number: str) -> RepairOrder:
        return RepairOrder.from_dict(json.loads(self._path(number).read_text(encoding="utf-8")))
