"""Облік витрат на Claude API: токени й вартість кожного запиту."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

# Ціни, $ за 1 млн токенів: (вхід, вихід, читання з кешу, запис у кеш на 5 хв).
# Запис у кеш — 1,25 × ціни входу. Перевіряйте актуальні ціни на anthropic.com/pricing.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-8": (5.00, 25.00, 0.50, 6.25),
}
DEFAULT_PRICE = PRICES["claude-opus-5-5"]
FIELDS = ("requests", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd")


def empty() -> dict:
    return {k: 0 for k in FIELDS}


def add(total: dict, extra: dict) -> dict:
    """Додати витрати extra до total (на місці) і повернути total."""
    for k in FIELDS:
        total[k] = total.get(k, 0) + extra.get(k, 0)
    total["cost_usd"] = round(total["cost_usd"], 6)
    return total


def price_for(model: str) -> tuple[float, float, float, float]:
    for name, price in PRICES.items():
        if model == name or model.startswith(name + "-"):
            return price
    return DEFAULT_PRICE


def from_response(usage, model: str) -> dict:
    """Витрати одного запиту з usage відповіді API."""
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    p_in, p_out, p_read, p_write = price_for(model or "")
    cost = (inp * p_in + out * p_out + read * p_read + write * p_write) / 1_000_000
    return {"requests": 1, "input_tokens": inp, "output_tokens": out, "cache_read_tokens": read,
            "cache_write_tokens": write, "cost_usd": round(cost, 6)}


class UsageLedger:
    """Журнал витрат: один рядок JSON на подію (повідомлення в чаті чи розпізнавання фото)."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._memory: list[dict] = []  # якщо файлу немає (тести, локальний запуск без диска)

    def record(self, kind: str, usage: dict, model: str, order: str | None) -> None:
        if not usage.get("requests"):
            return
        entry = {"at": datetime.now().isoformat(timespec="seconds"), "kind": kind, "model": model,
                 "order": order, **{k: usage.get(k, 0) for k in FIELDS}}
        with self._lock:
            if self.path is None:
                self._memory.append(entry)
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def entries(self) -> list[dict]:
        with self._lock:
            if self.path is None:
                return list(self._memory)
            if not self.path.exists():
                return []
            lines = self.path.read_text(encoding="utf-8").splitlines()
        result = []
        for line in lines:
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # пошкоджений рядок (наприклад, обірваний запис) пропускаємо
        return result

    def summary(self, days: int = 14) -> dict:
        today = datetime.now().date()
        month = today.strftime("%Y-%m")
        totals = {"today": empty(), "month": empty(), "all": empty()}
        by_day = {(today - timedelta(days=i)).isoformat(): empty() for i in range(days)}
        orders: dict[str, float] = {}
        for e in self.entries():
            day = e.get("at", "")[:10]
            add(totals["all"], e)
            if day.startswith(month):
                add(totals["month"], e)
            if day == today.isoformat():
                add(totals["today"], e)
            if day in by_day:
                add(by_day[day], e)
            if e.get("order"):
                orders[e["order"]] = orders.get(e["order"], 0) + e.get("cost_usd", 0)
        return {
            **totals,
            "orders": len(orders),
            "avg_per_order": round(sum(orders.values()) / len(orders), 4) if orders else 0,
            "by_day": [{"day": d, **v} for d, v in sorted(by_day.items())],
        }
