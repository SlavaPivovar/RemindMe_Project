"""Закупівлі: замовлення постачальникам і прийом запчастин на склад із закупівельною ціною.

Замовлення постачальнику (ЗП-<рік>-<номер>) складається з довідника запчастин — вручну або зі
списку «Що замовити». Під час приймання товар іде на склад разом із закупівельною ціною, з якої
рахується середня собівартість запчастини і, отже, маржа на запчастинах у замовленнях-нарядах.
Зберігається в purchases.json поруч із каталогом orders/.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .order import money, now_str

DRAFT, ORDERED, PARTIAL, RECEIVED, CANCELLED = "чернетка", "замовлено", "отримано частково", "отримано", "скасовано"
STATUSES = (DRAFT, ORDERED, PARTIAL, RECEIVED, CANCELLED)


class PurchaseError(ValueError):
    """Текст показується користувачу."""


def _num(value, field: str, positive: bool = True) -> float:
    try:
        num = float(str(value).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        raise PurchaseError(f"{field}: потрібне число") from None
    if num != num or num < 0 or (positive and num == 0) or num > 1_000_000:
        raise PurchaseError(f"{field}: некоректне значення")
    return round(num, 3)


class PurchaseStore:
    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._memory: list[dict] = []

    def _read(self) -> list[dict]:
        if self.path is None:
            return self._memory
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("purchases", [])
        except (FileNotFoundError, ValueError):
            return []

    def _write(self, items: list[dict]) -> None:
        if self.path is None:
            self._memory = items
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps({"purchases": items}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def _total(po: dict) -> str:
        return str(money(sum((Decimal(str(x["qty"])) * Decimal(str(x["cost"])) for x in po["lines"]), Decimal("0"))))

    def _view(self, po: dict) -> dict:
        return {**po, "total": self._total(po)}

    def list(self) -> list[dict]:
        return [self._view(p) for p in sorted(self._read(), key=lambda p: p["number"], reverse=True)]

    def get(self, po_id: str) -> dict:
        po = next((p for p in self._read() if p["id"] == po_id), None)
        if po is None:
            raise KeyError(po_id)
        return self._view(po)

    def suppliers(self) -> list[str]:
        return sorted({p["supplier"] for p in self._read() if p["supplier"]})

    def _clean_lines(self, lines, parts: dict) -> list[dict]:
        if not isinstance(lines, list) or not lines:
            raise PurchaseError("Додайте хоча б одну запчастину")
        result = {}
        for x in lines:
            part_no = str(x.get("part_no") or "").strip().upper()
            if part_no not in parts:
                raise PurchaseError(f"Немає запчастини {part_no or '—'} у довіднику")
            qty = _num(x.get("qty"), f"{part_no}: кількість")
            cost = _num(x.get("cost") or 0, f"{part_no}: ціна закупівлі", positive=False)
            if part_no in result:  # той самий артикул двічі — зводимо в один рядок
                result[part_no]["qty"] = round(result[part_no]["qty"] + qty, 3)
                continue
            result[part_no] = {"part_no": part_no, "name": parts[part_no].name, "unit": parts[part_no].unit,
                               "qty": qty, "cost": cost, "received": float(x.get("received") or 0)}
        return list(result.values())

    def create(self, data: dict, parts: dict, by: str = "") -> dict:
        year = datetime.now().year
        with self._lock:
            items = self._read()
            nums = [int(p["number"].rsplit("-", 1)[1]) for p in items if p["number"].startswith(f"ЗП-{year}-")]
            po = {"id": secrets.token_hex(4), "number": f"ЗП-{year}-{max(nums, default=0) + 1:03d}",
                  "supplier": str(data.get("supplier") or "").strip()[:100], "note": str(data.get("note") or "").strip()[:500],
                  "status": DRAFT, "created_at": now_str(), "by": by[:80], "ordered_at": "", "received_at": "",
                  "lines": self._clean_lines(data.get("lines"), parts)}
            items.append(po)
            self._write(items)
        return self._view(po)

    def update(self, po_id: str, data: dict, parts: dict) -> dict:
        with self._lock:
            items = self._read()
            po = next((p for p in items if p["id"] == po_id), None)
            if po is None:
                raise KeyError(po_id)
            if "lines" in data:
                if po["status"] not in (DRAFT, ORDERED):
                    raise PurchaseError("Отримане замовлення змінювати не можна")
                received = {x["part_no"]: x["received"] for x in po["lines"]}
                po["lines"] = [{**x, "received": received.get(x["part_no"], 0)} for x in self._clean_lines(data["lines"], parts)]
            for key, limit in (("supplier", 100), ("note", 500)):
                if key in data:
                    po[key] = str(data[key] or "").strip()[:limit]
            if "status" in data and data["status"] != po["status"]:
                if data["status"] not in (ORDERED, CANCELLED, DRAFT):
                    raise PurchaseError("Цей статус ставиться автоматично під час приймання")
                if po["status"] in (RECEIVED, PARTIAL) and data["status"] != po["status"]:
                    raise PurchaseError("Товар уже приймали — статус змінити не можна")
                po["status"] = data["status"]
                if data["status"] == ORDERED:
                    po["ordered_at"] = now_str()
            self._write(items)
        return self._view(po)

    def receive(self, po_id: str, lines: list[dict]) -> tuple[dict, list[dict]]:
        """Прийняти товар: lines — [{part_no, qty, cost}]. Повертає (замовлення, що прийнято) для складу."""
        with self._lock:
            items = self._read()
            po = next((p for p in items if p["id"] == po_id), None)
            if po is None:
                raise KeyError(po_id)
            if po["status"] in (RECEIVED, CANCELLED):
                raise PurchaseError(f"Замовлення вже {po['status']}")
            by_no = {x["part_no"]: x for x in po["lines"]}
            got = []
            for x in lines:
                line = by_no.get(str(x.get("part_no") or "").upper())
                if line is None:
                    raise PurchaseError("Цієї запчастини немає в замовленні")
                qty = _num(x.get("qty") or 0, f"{line['part_no']}: отримано", positive=False)
                if not qty:
                    continue
                cost = _num(x.get("cost") if x.get("cost") not in (None, "") else line["cost"],
                            f"{line['part_no']}: ціна", positive=False)
                line["received"] = round(line["received"] + qty, 3)
                line["cost"] = cost
                got.append({"part_no": line["part_no"], "qty": qty, "cost": cost})
            if not got:
                raise PurchaseError("Вкажіть, скільки отримано")
            po["status"] = RECEIVED if all(x["received"] >= x["qty"] for x in po["lines"]) else PARTIAL
            po["received_at"] = now_str()
            self._write(items)
        return self._view(po), got
