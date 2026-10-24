"""Довідник робіт і запчастин."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_CATALOG = Path(__file__).parent / "data" / "catalog.json"
MODEL_CODES = ("MODEL_S", "MODEL_3", "MODEL_X", "MODEL_Y", "CYBERTRUCK")
CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,39}$")


class CatalogError(ValueError):
    """Некоректні дані довідника; текст повідомлення показується користувачу."""


@dataclass
class Operation:
    code: str
    name: str
    price: float  # фіксована ціна роботи
    models: list[str]
    parts: list[str]
    category: str = ""
    interval: str = ""  # рекомендований інтервал обслуговування, якщо є

    def fits(self, model_code: str | None) -> bool:
        return "*" in self.models or model_code is None or model_code in self.models


@dataclass
class Part:
    part_no: str
    name: str
    price: float
    unit: str = "шт"


class Catalog:
    def __init__(self, data: dict, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.service: dict = data.get("service", {})
        self.currency: str = data.get("currency", "$")
        self.vat_percent: float = data.get("vat_percent", 0)
        self.prices_include_vat: bool = data.get("prices_include_vat", True)
        # Старий формат довідника (нормогодини × ставка) переводиться у фіксовані ціни.
        legacy_rate = data.get("labor_rate_per_hour", 0)
        self.operations = {}
        for o in data["operations"]:
            o = dict(o)
            if "hours" in o:
                hours = o.pop("hours")
                o.setdefault("price", round(hours * legacy_rate, 2))
            self.operations[o["code"]] = Operation(**o)
        self.parts = {p["part_no"]: Part(**p) for p in data["parts"]}

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CATALOG) -> "Catalog":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), path)

    @classmethod
    def load_editable(cls, path: str | Path, default: str | Path = DEFAULT_CATALOG) -> "Catalog":
        """Завантажити довідник, який можна редагувати. Якщо файлу ще немає (перший запуск),
        він створюється як копія стандартного довідника."""
        path = Path(path)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(default, path)
        catalog = cls.load(path)
        if "labor_rate_per_hour" in json.loads(path.read_text(encoding="utf-8")):
            catalog.save()  # файл старого формату одразу переписуємо з фіксованими цінами
        return catalog

    # --- збереження ---

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "currency": self.currency,
            "vat_percent": self.vat_percent,
            "prices_include_vat": self.prices_include_vat,
            "operations": [asdict(o) for o in self.operations.values()],
            "parts": [asdict(p) for p in self.parts.values()],
        }

    def save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)  # атомарно: файл не залишиться напівзаписаним

    def _commit(self, **changes) -> None:
        """Застосувати зміни й записати файл; якщо запис не вдався — повернути як було.
        Словники замінюються новими, а не змінюються на місці, тож паралельні читачі
        (пошук помічника, сторінка довідника) не натрапляють на зміну під час перебору."""
        old = {k: getattr(self, k) for k in changes}
        for k, v in changes.items():
            setattr(self, k, v)
        try:
            self.save()
        except OSError:
            for k, v in old.items():
                setattr(self, k, v)
            raise

    # --- редагування ---

    @staticmethod
    def _text(value, field: str, limit: int, required: bool = True) -> str:
        value = str(value or "").strip()
        if required and not value:
            raise CatalogError(f"Поле «{field}» обов'язкове")
        if len(value) > limit:
            raise CatalogError(f"Поле «{field}» задовге (до {limit} символів)")
        return value

    @staticmethod
    def _number(value, field: str, low: float, high: float, strict_low: bool = False) -> float:
        try:
            num = float(str(value).replace(",", "."))
        except (TypeError, ValueError):
            raise CatalogError(f"Поле «{field}» має бути числом") from None
        if num != num or num < low or (strict_low and num == low) or num > high:
            raise CatalogError(f"Поле «{field}» має бути в межах від {low:g} до {high:g}")
        return round(num, 2)

    def _code(self, code: str, field: str) -> str:
        code = str(code or "").strip().upper()
        if not CODE_RE.match(code):
            raise CatalogError(f"{field}: лише латинські літери, цифри та дефіс, від 2 до 40 символів")
        return code

    def save_operation(self, data: dict, create: bool) -> Operation:
        code = self._code(data.get("code"), "Код роботи")
        models = data.get("models") or []
        if not isinstance(models, list) or not models:
            raise CatalogError("Вкажіть хоча б одну модель або «усі»")
        models = ["*"] if "*" in models else [m for m in MODEL_CODES if m in models]
        if not models:
            raise CatalogError("Невідома модель")
        parts = data.get("parts") or []
        if not isinstance(parts, list):
            raise CatalogError("Запчастини мають бути списком артикулів")
        parts = list(dict.fromkeys(str(p).strip().upper() for p in parts if str(p).strip()))
        unknown = [p for p in parts if p not in self.parts]
        if unknown:
            raise CatalogError(f"Немає таких запчастин у довіднику: {', '.join(unknown)}")
        op = Operation(
            code=code,
            name=self._text(data.get("name"), "Назва", 200),
            price=self._number(data.get("price"), "Ціна", 0, 1_000_000),
            models=models,
            parts=parts,
            category=self._text(data.get("category"), "Розділ", 60),
            interval=self._text(data.get("interval"), "Інтервал", 200, required=False),
        )
        with self.lock:
            exists = code in self.operations
            if create and exists:
                raise CatalogError(f"Робота з кодом {code} уже є")
            if not create and not exists:
                raise KeyError(code)
            self._commit(operations={**self.operations, code: op})
        return op

    def delete_operation(self, code: str) -> None:
        with self.lock:
            if code not in self.operations:
                raise KeyError(code)
            self._commit(operations={k: v for k, v in self.operations.items() if k != code})

    def save_part(self, data: dict, create: bool) -> Part:
        part_no = self._code(data.get("part_no"), "Артикул")
        part = Part(
            part_no=part_no,
            name=self._text(data.get("name"), "Назва", 200),
            price=self._number(data.get("price"), "Ціна", 0, 1_000_000),
            unit=self._text(data.get("unit") or "шт", "Одиниця", 10),
        )
        with self.lock:
            exists = part_no in self.parts
            if create and exists:
                raise CatalogError(f"Запчастина з артикулом {part_no} уже є")
            if not create and not exists:
                raise KeyError(part_no)
            self._commit(parts={**self.parts, part_no: part})
        return part

    def delete_part(self, part_no: str) -> None:
        with self.lock:
            if part_no not in self.parts:
                raise KeyError(part_no)
            used = [o.code for o in self.operations.values() if part_no in o.parts]
            if used:
                raise CatalogError(f"Запчастина використовується в роботах: {', '.join(used)}. "
                                   "Спершу приберіть її з цих робіт.")
            self._commit(parts={k: v for k, v in self.parts.items() if k != part_no})

    def update_settings(self, data: dict) -> None:
        service = {
            "name": self._text(data.get("name"), "Назва сервісу", 100),
            "address": self._text(data.get("address"), "Адреса", 200, required=False),
            "phone": self._text(data.get("phone"), "Телефон", 50, required=False),
            "inn": self._text(data.get("inn"), "ЄДРПОУ", 20, required=False),
        }
        vat = self._number(data.get("vat_percent"), "ПДВ", 0, 100)
        with self.lock:
            self._commit(service=service, vat_percent=vat,
                         prices_include_vat=bool(data.get("prices_include_vat", self.prices_include_vat)))

    def search_operations(self, query: str, model_code: str | None = None) -> list[Operation]:
        words = [w for w in query.lower().split() if len(w) > 2]
        scored = []
        for op in self.operations.values():
            if not op.fits(model_code):
                continue
            hay = f"{op.code} {op.name} {op.category}".lower()
            score = sum(w[:5] in hay for w in words)
            if not words or score:
                scored.append((score, op))
        # Спершу роботи, що збіглися з більшою кількістю слів запиту.
        scored.sort(key=lambda x: -x[0])
        return [op for _, op in scored]

    def categories(self) -> list[str]:
        return list(dict.fromkeys(op.category for op in self.operations.values() if op.category))

    def search_parts(self, query: str) -> list[Part]:
        words = [w for w in query.lower().split() if len(w) > 2]
        return [
            p for p in self.parts.values()
            if not words or any(w[:5] in f"{p.part_no} {p.name}".lower() for w in words)
        ]
