"""Склад запчастин: залишки, прихід, інвентаризація, списання за замовленнями-нарядами.

Запчастини з довідника списуються, коли замовлення-наряд отримує статус «видано», і
повертаються на склад, якщо статус змінили назад чи прибрали рядок. Списання прив'язане до
замовлення (stock.json → orders), тож повторні збереження нічого не списують удруге, а правка
виданого замовлення списує лише різницю. Власні позиції (не з довідника) на склад не впливають.

Залишки — у stock.json, рух — у stock_moves.jsonl поруч (кожен рядок — одна операція).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .order import now_str

ISSUED = "видано"
RECEIPT, ADJUST, WRITE_OFF, RETURN = "прихід", "інвентаризація", "списання", "повернення"
MAX_QTY = 1_000_000


class StockError(ValueError):
    """Текст показується користувачу."""


def _q(value) -> float:
    """Кількість без «0.30000000000000004»."""
    return float(Decimal(str(value)).quantize(Decimal("0.001")).normalize())


def _number(value, field: str, allow_zero: bool = True, allow_negative: bool = False) -> float:
    try:
        num = float(str(value).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        raise StockError(f"{field}: потрібне число") from None
    if num != num or abs(num) > MAX_QTY or (not allow_negative and num < 0) or (not allow_zero and num == 0):
        raise StockError(f"{field}: некоректне значення")
    return _q(num)


class Stock:
    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self.moves_path = self.path.with_name("stock_moves.jsonl") if self.path else None
        self._lock = threading.RLock()
        self._data = self._load()
        self._moves_memory: list[dict] = []
        if self.path is not None and not self.path.exists():
            self._save()  # одразу запам'ятати, з якого моменту ведемо склад

    def _load(self) -> dict:
        data = {}
        if self.path is not None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (FileNotFoundError, ValueError):
                data = {}
        # since — коли почали вести склад: замовлення, видані раніше, не списуються заднім числом.
        return {"items": data.get("items", {}), "orders": data.get("orders", {}),
                "since": data.get("since") or datetime.now().strftime("%Y-%m-%d %H:%M")}

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def _log(self, move: dict) -> None:
        if self.moves_path is None:
            self._moves_memory.append(move)
            return
        with self.moves_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(move, ensure_ascii=False) + "\n")

    def _apply(self, part_no: str, delta: float, kind: str, by: str = "", note: str = "", order: str = "") -> dict:
        item = self._data["items"].setdefault(part_no, {"qty": 0, "min": 0})
        item["qty"] = _q(item["qty"] + delta)
        move = {"at": now_str(), "part_no": part_no, "qty": _q(delta), "after": item["qty"], "kind": kind,
                "by": by[:80], "note": note[:200], "order": order}
        self._log(move)
        return move

    # --- читання ---

    def qty(self, part_no: str) -> float:
        return self._data["items"].get(part_no, {}).get("qty", 0)

    def min(self, part_no: str) -> float:
        return self._data["items"].get(part_no, {}).get("min", 0)

    def is_low(self, part_no: str) -> bool:
        m = self.min(part_no)
        return m > 0 and self.qty(part_no) <= m

    def moves(self, part_no: str | None = None, limit: int = 200) -> list[dict]:
        if self.moves_path is None:
            rows = list(self._moves_memory)
        else:
            try:
                rows = [json.loads(line) for line in self.moves_path.read_text(encoding="utf-8").splitlines() if line]
            except FileNotFoundError:
                rows = []
        if part_no:
            rows = [r for r in rows if r["part_no"] == part_no]
        return rows[-limit:][::-1]  # нові згори

    # --- операції ---

    def receive(self, part_no: str, qty, by: str = "", note: str = "") -> dict:
        amount = _number(qty, "Кількість", allow_zero=False)
        with self._lock:
            move = self._apply(part_no, amount, RECEIPT, by, note)
            self._save()
        return move

    def adjust(self, part_no: str, actual, by: str = "", note: str = "") -> dict | None:
        """Інвентаризація: фактичний залишок. Повертає операцію або None, якщо залишок не змінився."""
        target = _number(actual, "Фактичний залишок")
        with self._lock:
            delta = _q(target - self.qty(part_no))
            if delta == 0:
                return None
            move = self._apply(part_no, delta, ADJUST, by, note)
            self._save()
        return move

    def set_min(self, part_no: str, value) -> None:
        minimum = _number(value, "Мінімальний залишок")
        with self._lock:
            self._data["items"].setdefault(part_no, {"qty": 0, "min": 0})["min"] = minimum
            self._save()

    def _issued_before_start(self, order) -> bool:
        issued = next((e["at"] for e in reversed(order.status_log) if e.get("status") == ISSUED), "")
        try:
            return datetime.strptime(issued, "%d.%m.%Y %H:%M").strftime("%Y-%m-%d %H:%M") < self._data["since"]
        except ValueError:
            return True  # дати видачі немає (старі дані) — не чіпаємо

    def sync_order(self, order, catalog_parts) -> list[str]:
        """Привести списання за замовленням у відповідність до його стану.
        Повертає артикули, залишок яких щойно опустився до мінімуму (для сповіщення)."""
        want: dict[str, float] = {}
        if order.status == ISSUED and order.number not in self._data["orders"] and self._issued_before_start(order):
            return []
        if order.status == ISSUED:
            for line in order.parts:
                if line.part_no in catalog_parts:
                    want[line.part_no] = _q(want.get(line.part_no, 0) + line.qty)
        with self._lock:
            done = self._data["orders"].get(order.number, {})
            if want == done:
                return []
            became_low = []
            for part_no in sorted(set(want) | set(done)):
                delta = _q(want.get(part_no, 0) - done.get(part_no, 0))
                if delta == 0:
                    continue
                was_low = self.is_low(part_no)
                kind = WRITE_OFF if delta > 0 else RETURN
                self._apply(part_no, -delta, kind, note=order.customer.name, order=order.number)
                if self.is_low(part_no) and not was_low:
                    became_low.append(part_no)
            if want:
                self._data["orders"][order.number] = want
            else:
                self._data["orders"].pop(order.number, None)
            self._save()
        return became_low
