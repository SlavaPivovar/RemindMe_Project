"""Модель замовлення-наряду та сховище."""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path


def money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


log = logging.getLogger(__name__)

# Життєвий цикл замовлення-наряду.
DRAFT = "чернетка"
CANCELLED = "скасовано"
STATUSES = (DRAFT, "прийнято", "в роботі", "готово", "видано", CANCELLED)
# Статуси попередніх версій програми.
LEGACY_STATUSES = {"відкрито": "прийнято", "виконано": "готово", "закрито": "видано"}


PAYMENT_METHODS = ("готівка", "картка", "переказ")


class PaymentError(ValueError):
    """Оплату не можна записати; текст показується користувачу."""


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
    status_log: list[dict] = field(default_factory=list)  # [{"status": ..., "at": "дд.мм.рррр гг:хх", "by": хто}]
    assignee: str = ""  # відповідальний майстер
    assignee_tg: int | None = None  # його Telegram id, якщо призначився кнопкою в групі
    # Оплати: [{"id", "at", "amount" (рядок, «-» — повернення), "method", "by", "note"}]
    payments: list[dict] = field(default_factory=list)
    # Акт приймання автомобіля: {"items": [...], "left_inside", "damages", "signed_at", "signed_by", "signature"}
    intake: dict = field(default_factory=dict)
    ai_usage: dict = field(default_factory=dict)  # витрати на Claude по цьому замовленню (див. usage.py)

    def set_status(self, status: str, by: str = "") -> None:
        status = LEGACY_STATUSES.get(status.strip().lower(), status.strip().lower())
        if status not in STATUSES:
            raise StatusError(f"Невідомий статус «{status}». Можливі: {', '.join(STATUSES)}")
        missing = self.missing_fields()
        if missing and status not in (DRAFT, CANCELLED):
            raise StatusError(f"Не можна встановити статус «{status}»: не заповнено — {', '.join(missing)}")
        if status != self.status:
            self.status = status
            self.status_log.append({"status": status, "at": now_str(), **({"by": by} if by else {})})

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

    def paid(self) -> Decimal:
        return money(sum((Decimal(p["amount"]) for p in self.payments), Decimal("0")))

    def balance(self) -> Decimal:
        """Скільки ще має сплатити клієнт (від'ємне — переплата)."""
        return money(self.totals()["total"] - self.paid())

    def payment_status(self) -> str:
        paid, balance = self.paid(), self.balance()
        if balance < 0:
            return "переплата"
        if balance == 0 and (paid > 0 or self.totals()["total"] > 0):
            return "оплачено"
        return "частково" if paid > 0 else "не оплачено"

    def add_payment(self, amount, method: str, by: str = "", note: str = "", uah: dict | None = None) -> dict:
        """uah — оплата гривнею: {"amount": сума в грн, "rate": курс}; amount тоді — уже перерахована сума в $."""
        try:
            value = money(Decimal(str(amount).replace(",", ".").replace(" ", "")))
        except (ArithmeticError, ValueError):
            raise PaymentError("Сума має бути числом") from None
        if value == 0 or abs(value) > Decimal("10000000"):
            raise PaymentError("Вкажіть суму оплати (від'ємна — повернення клієнту)")
        if method not in PAYMENT_METHODS:
            raise PaymentError(f"Спосіб оплати: {', '.join(PAYMENT_METHODS)}")
        payment = {"id": secrets.token_hex(4), "at": now_str(), "amount": str(value), "method": method,
                   "by": by.strip()[:80], "note": note.strip()[:200],
                   **({"amount_uah": str(uah["amount"]), "rate": str(uah["rate"])} if uah else {})}
        self.payments.append(payment)
        return payment

    def remove_payment(self, payment_id: str) -> dict:
        payment = next((p for p in self.payments if p["id"] == payment_id), None)
        if payment is None:
            raise KeyError(payment_id)
        self.payments.remove(payment)
        return payment

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
        data["paid"], data["balance"] = str(self.paid()), str(self.balance())
        data["payment_status"] = self.payment_status()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "RepairOrder":
        data = dict(data)
        for key in ("totals", "paid", "balance", "payment_status"):
            data.pop(key, None)
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
        self.listeners: list = []  # викликаються після кожного збереження: listener(order)

    def next_number(self) -> str:
        """Видати новий номер. Номер резервується, навіть якщо замовлення ще не збережене,
        щоб паралельні сеанси не отримали однакові номери."""
        year = datetime.now().year
        prefix = f"ЗН-{year}-"
        with self._lock:
            taken = {p.stem for p in self.dir.glob(f"{prefix}*.json")} | self._reserved
            # Номери видалених замовлень теж зайняті: номер не повинен дістатися іншому замовленню.
            taken |= {p.name.split(".")[0] for p in self.trash.glob(f"{prefix}*.json")}
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

    @property
    def trash(self) -> Path:
        return self.dir / "deleted"

    def is_deleted(self, number: str) -> bool:
        return any(p.name.split(".")[0] == number for p in self.trash.glob(f"{number}*.json"))

    def save(self, order: RepairOrder) -> Path:
        path = self._path(order.number)
        with self._lock:
            if self.is_deleted(order.number):
                # Замовлення видалили, поки воно було відкрите деінде, — не воскрешаємо його.
                raise FileNotFoundError(f"Замовлення-наряд {order.number} видалено")
            # Пишемо в тимчасовий файл і атомарно підміняємо: при збої не лишиться напівзаписаного файлу.
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(order.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        for listener in self.listeners:
            try:
                listener(order)
            except Exception:  # сповіщення (Telegram) не повинні ламати збереження
                log.exception("Помилка обробника збереження")
        return path

    def delete(self, number: str) -> None:
        """Перенести замовлення-наряд у кошик (orders/deleted/); звідти його можна повернути вручну."""
        path = self._path(number)
        with self._lock:
            if not path.exists():
                raise FileNotFoundError(number)
            self.trash.mkdir(exist_ok=True)
            target = self.trash / path.name
            if target.exists():
                target = self.trash / f"{number}.{datetime.now():%Y%m%d-%H%M%S}.json"
            os.replace(path, target)
            path.with_suffix(".html").unlink(missing_ok=True)  # друковану форму завжди можна згенерувати знову

    def history(self, vin: str, exclude: str | None = None) -> list[RepairOrder]:
        """Попередні замовлення-наряди автомобіля з цим VIN, від найновішого."""
        vin = vin.strip().upper().replace(" ", "")
        if not vin:
            return []
        found = [o for o in self.list() if o.vehicle.vin == vin and o.number != exclude]
        return sorted(found, key=lambda o: _parse_date(o.created_at), reverse=True)

    def load(self, number: str) -> RepairOrder:
        return RepairOrder.from_dict(json.loads(self._path(number).read_text(encoding="utf-8")))
