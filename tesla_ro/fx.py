"""Гривня поруч із доларом: курс НБУ (або курс сервісу) і перерахунок сум.

Курс НБУ береться з відкритого API банку раз на день і зберігається у fx.json: якщо банк
недоступний, використовується останній відомий курс. Налаштування (Довідник → Налаштування):
не показувати / курс НБУ (з необов'язковою націнкою, %) / свій курс.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

NBU_URL = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode=USD&json"
MODES = ("off", "nbu", "manual")
RETRY_AFTER = 30 * 60  # якщо банк не відповів — наступна спроба не раніше ніж за 30 хв


def uah(amount, rate: Decimal) -> Decimal:
    return (Decimal(str(amount)) * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def to_usd(amount_uah, rate: Decimal) -> Decimal:
    return (Decimal(str(amount_uah)) / rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class Rates:
    def __init__(self, path: str | Path | None, fetch=None):
        self.path = Path(path) if path else None
        self._fetch = fetch or self._fetch_nbu  # () -> (Decimal курс, "дд.мм.рррр")
        self._lock = threading.Lock()
        self._cache = self._load()
        self._last_try = 0.0
        self._refreshing = False

    def _load(self) -> dict:
        if self.path is None:
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def _fetch_nbu():
        response = httpx.get(NBU_URL, timeout=5)
        response.raise_for_status()
        row = next(r for r in response.json() if r.get("cc") == "USD")
        return Decimal(str(row["rate"])), row.get("exchangedate", "")

    def _refresh(self) -> None:
        try:
            rate, day = self._fetch()
            with self._lock:
                self._cache = {"rate": str(rate), "date": day, "fetched": date.today().isoformat()}
                self._save()
        except Exception as exc:  # мережа, формат відповіді — працюємо з останнім відомим курсом
            log.warning("Курс НБУ недоступний: %s", exc)
        finally:
            self._refreshing = False

    def nbu(self) -> tuple[Decimal, str] | None:
        """Курс НБУ: сьогоднішній, якщо вдалося отримати, інакше останній відомий."""
        stale = self._cache.get("fetched") != date.today().isoformat()
        waited = time.monotonic() - self._last_try
        # Курсу ще немає — пробуємо щохвилини; застарів — раз на 30 хв (банк може бути недоступний).
        due = waited > (RETRY_AFTER if self._cache else 60) or not self._last_try
        if stale and due and not self._refreshing:
            self._last_try = time.monotonic()
            self._refreshing = True
            if self._cache.get("rate"):  # є старий курс — оновлюємо у фоні, не затримуючи сторінку
                threading.Thread(target=self._refresh, daemon=True).start()
            else:
                self._refresh()
        if not self._cache.get("rate"):
            return None
        return Decimal(self._cache["rate"]), self._cache.get("date", "")

    def current(self, settings: dict) -> dict | None:
        """Курс за налаштуваннями сервісу: {"rate", "date", "source", "nbu"} або None (гривня вимкнена/недоступна)."""
        mode = settings.get("mode", "nbu")
        if mode == "off":
            return None
        if mode == "manual":
            try:
                rate = Decimal(str(settings.get("manual_rate") or 0))
            except ArithmeticError:
                return None
            if rate <= 0:
                return None
            return {"rate": rate.quantize(Decimal("0.0001")), "date": datetime.now().strftime("%d.%m.%Y"),
                    "source": "курс сервісу", "nbu": None}
        found = self.nbu()
        if found is None:
            return None
        nbu_rate, day = found
        markup = Decimal(str(settings.get("markup") or 0))
        rate = (nbu_rate * (1 + markup / 100)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        return {"rate": rate, "date": day, "source": "курс НБУ" + (f" + {markup.normalize():f}%" if markup else ""),
                "nbu": nbu_rate}


def describe(info: dict | None, amount) -> dict | None:
    """Готові для інтерфейсу рядки: сума в гривні й підпис курсу."""
    if not info:
        return None
    return {"amount": str(uah(amount, info["rate"])), "rate": str(info["rate"].normalize()), "date": info["date"],
            "source": info["source"]}
