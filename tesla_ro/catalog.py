"""Справочник работ и запчастей."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CATALOG = Path(__file__).parent / "data" / "catalog.json"


@dataclass
class Operation:
    code: str
    name: str
    hours: float
    models: list[str]
    parts: list[str]

    def fits(self, model_code: str | None) -> bool:
        return "*" in self.models or model_code is None or model_code in self.models


@dataclass
class Part:
    part_no: str
    name: str
    price: float
    unit: str = "шт"


class Catalog:
    def __init__(self, data: dict):
        self.service: dict = data.get("service", {})
        self.currency: str = data.get("currency", "₽")
        self.vat_percent: float = data.get("vat_percent", 0)
        self.prices_include_vat: bool = data.get("prices_include_vat", True)
        self.labor_rate: float = data["labor_rate_per_hour"]
        self.operations = {o["code"]: Operation(**o) for o in data["operations"]}
        self.parts = {p["part_no"]: Part(**p) for p in data["parts"]}

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CATALOG) -> "Catalog":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def search_operations(self, query: str, model_code: str | None = None) -> list[Operation]:
        words = [w for w in query.lower().split() if len(w) > 2]
        found = []
        for op in self.operations.values():
            if not op.fits(model_code):
                continue
            hay = f"{op.code} {op.name}".lower()
            if not words or any(w[:5] in hay for w in words):
                found.append(op)
        return found

    def search_parts(self, query: str) -> list[Part]:
        words = [w for w in query.lower().split() if len(w) > 2]
        return [
            p for p in self.parts.values()
            if not words or any(w[:5] in f"{p.part_no} {p.name}".lower() for w in words)
        ]
