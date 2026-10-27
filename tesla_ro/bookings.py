"""Запис клієнтів на сервіс: планувальник і помічник, що записує з вільного тексту.

Запис — це лише час і домовленість («Петренко, Model Y, пт 10:00, ТО і стукіт»), а не
замовлення-наряд: замовлення створюється вручну, коли клієнт приїхав. Обмежень на кількість
записів на один час немає — помічник лише попереджає про накладки.

Записи зберігаються в bookings.json поруч із каталогом orders/.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from datetime import date, datetime
from pathlib import Path

import anthropic
from anthropic import beta_tool
from anthropic.lib.tools import ToolError

from . import usage as usage_mod
from .order import now_str

PLANNED, ARRIVED, NO_SHOW, CANCELLED = "заплановано", "приїхав", "не приїхав", "скасовано"
STATUSES = (PLANNED, ARRIVED, NO_SHOW, CANCELLED)
WEEKDAYS = ("понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя")
WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")
FIELDS = ("client_name", "phone", "car", "work", "note")
LIMITS = {"client_name": 100, "phone": 30, "car": 120, "work": 500, "note": 500}


class BookingError(ValueError):
    """Некоректні дані запису; текст показується користувачу."""


def parse_date(value: str) -> date:
    value = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise BookingError(f"Некоректна дата «{value}» (потрібно РРРР-ММ-ДД)")


def parse_time(value: str) -> str:
    value = str(value or "").strip().replace(".", ":")
    try:
        return datetime.strptime(value, "%H:%M").strftime("%H:%M")
    except ValueError:
        raise BookingError(f"Некоректний час «{value}» (потрібно ГГ:ХХ)") from None


def label(b: dict) -> str:
    """«пт 31.10 10:00» — коротко для повідомлень."""
    d = parse_date(b["date"])
    return f"{WEEKDAYS_SHORT[d.weekday()]} {d:%d.%m} {b['time']}"


def long_label(b: dict) -> str:
    """«п'ятниця, 31.10 о 10:00» — для клієнта."""
    d = parse_date(b["date"])
    return f"{WEEKDAYS[d.weekday()]}, {d:%d.%m} о {b['time']}"


def _minutes(b: dict) -> tuple[int, int]:
    h, m = map(int, b["time"].split(":"))
    start = h * 60 + m
    return start, start + int(b.get("duration_min") or 60)


class BookingStore:
    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._memory: list[dict] = []
        self.listeners: list = []  # listener(kind, booking): «created» / «updated» / «cancelled» / «status»

    def _read(self) -> list[dict]:
        if self.path is None:
            return self._memory
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("bookings", [])
        except (FileNotFoundError, ValueError):
            return []

    def _write(self, items: list[dict]) -> None:
        if self.path is None:
            self._memory = items
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps({"bookings": items}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def _notify(self, kind: str, booking: dict) -> None:
        for listener in self.listeners:
            try:
                listener(kind, dict(booking))
            except Exception:  # сповіщення не повинні ламати запис
                logging.getLogger(__name__).exception("Помилка обробника запису")

    @staticmethod
    def _clean(data: dict, partial: bool) -> dict:
        out = {}
        if "date" in data or not partial:
            out["date"] = parse_date(data.get("date")).isoformat()
        if "time" in data or not partial:
            out["time"] = parse_time(data.get("time"))
        if "duration_min" in data or not partial:
            try:
                duration = int(data.get("duration_min") or 60)
            except (TypeError, ValueError):
                raise BookingError("Тривалість має бути числом хвилин") from None
            if not 10 <= duration <= 12 * 60:
                raise BookingError("Тривалість — від 10 хвилин до 12 годин")
            out["duration_min"] = duration
        for key in FIELDS:
            if key in data or not partial:
                value = " ".join(str(data.get(key) or "").split()) if key != "note" else str(data.get(key) or "").strip()
                if len(value) > LIMITS[key]:
                    raise BookingError(f"Поле «{key}» задовге (до {LIMITS[key]} символів)")
                out[key] = value
        if not partial and not out["client_name"]:
            raise BookingError("Вкажіть клієнта")
        return out

    def list(self, date_from: str | None = None, date_to: str | None = None, include_cancelled: bool = False,
             query: str = "") -> list[dict]:
        items = self._read()
        words = query.lower().split()
        result = [b for b in items
                  if (include_cancelled or b["status"] != CANCELLED)
                  and (not date_from or b["date"] >= date_from) and (not date_to or b["date"] <= date_to)
                  and all(w in f"{b['client_name']} {b['phone']} {b['car']} {b['work']}".lower() for w in words)]
        return sorted(result, key=lambda b: (b["date"], b["time"]))

    def get(self, booking_id: str) -> dict:
        b = next((x for x in self._read() if x["id"] == booking_id), None)
        if b is None:
            raise KeyError(booking_id)
        return b

    def overlaps(self, booking: dict) -> list[dict]:
        start, end = _minutes(booking)
        return [b for b in self.list(booking["date"], booking["date"])
                if b["id"] != booking.get("id") and b["status"] == PLANNED
                and _minutes(b)[0] < end and start < _minutes(b)[1]]

    def create(self, data: dict, by: str = "", source: str = "web") -> dict:
        booking = {"id": secrets.token_hex(4), **self._clean(data, partial=False), "status": PLANNED,
                   "created_at": now_str(), "created_by": by[:80], "source": source, "order_number": "",
                   "history": []}
        with self._lock:
            items = self._read()
            items.append(booking)
            self._write(items)
        self._notify("created", booking)
        return booking

    def update(self, booking_id: str, data: dict, by: str = "") -> tuple[dict, dict]:
        """Повертає (було, стало)."""
        changes = self._clean(data, partial=True)
        with self._lock:
            items = self._read()
            booking = next((x for x in items if x["id"] == booking_id), None)
            if booking is None:
                raise KeyError(booking_id)
            if booking["status"] == CANCELLED:
                raise BookingError("Запис скасовано — створіть новий")
            before = dict(booking)
            booking.update(changes)
            if (booking["date"], booking["time"]) != (before["date"], before["time"]):
                booking["history"] = booking.get("history", []) + [
                    {"at": now_str(), "by": by[:80], "from": f"{before['date']} {before['time']}"}]
            self._write(items)
        if booking != before:
            self._notify("updated", {**booking, "_before": {k: before[k] for k in ("date", "time")}})
        return before, booking

    def set_status(self, booking_id: str, status: str, by: str = "", order_number: str = "") -> dict:
        if status not in STATUSES:
            raise BookingError(f"Невідомий статус запису. Можливі: {', '.join(STATUSES)}")
        with self._lock:
            items = self._read()
            booking = next((x for x in items if x["id"] == booking_id), None)
            if booking is None:
                raise KeyError(booking_id)
            old = booking["status"]
            booking["status"] = status
            if order_number:
                booking["order_number"] = order_number
            if status == CANCELLED:
                booking["cancelled_by"] = by[:80]
            self._write(items)
        if old != status:
            self._notify("cancelled" if status == CANCELLED else "status", booking)
        return booking


SYSTEM_PROMPT = """\
Ти — адміністратор запису сервісного центру{service} з ремонту автомобілів Tesla. Співробітники
пишуть тобі у вільній формі (часто коротко, надиктовано голосом), а ти ведеш планувальник записів:
створюєш, переносиш, змінюєш і скасовуєш записи. Відповідай українською, дуже коротко.

Сьогодні: {today}. Зараз: {now}.

Правила:
- Запис — це дата, час, клієнт і що треба зробити (вільним текстом). Тривалість — 60 хв, якщо не
  сказано інше. Автомобіль, телефон і примітку записуй, якщо названо. Замовлення-наряди ти не
  створюєш — лише запис у планувальнику.
- Дати на кшталт «завтра», «в п'ятницю», «наступного вівторка» переводь у конкретну дату від
  сьогоднішньої (найближча така дата в майбутньому). Час «о 10», «на десяту ранку» — 10:00;
  «о третій» без уточнення — швидше 15:00 (робочий день). Минулих дат не буває.
- Якщо бракує дня, часу чи клієнта — спитай саме це, нічого не створюючи.
- Клієнта шукай у базі (find_client) за прізвищем, телефоном чи номером авто, щоб підставити
  телефон і автомобіль. Якщо знайдено кількох — уточни, кого саме. Телефон не вигадуй.
- Обмежень на кількість машин одночасно немає. Якщо інструмент повідомив про накладку з іншими
  записами — коротко згадай про неї, але запис залиш.
- Щоб перенести чи скасувати, спершу знайди запис (list_bookings) і зміни його (update_booking /
  cancel_booking). Якщо підходить кілька — уточни.
- Після дії підтверди одним рядком: що, коли (день тижня, дата, час), хто. Без зайвих пояснень.
"""


class BookingTools:
    """Інструменти помічника запису."""

    def __init__(self, store: BookingStore, clients=None, author: str = "", source: str = "web"):
        self.store = store
        self.clients = clients  # () -> list[dict] (clients.build_clients)
        self.author = author
        self.source = source

    @staticmethod
    def _public(b: dict) -> dict:
        d = parse_date(b["date"])
        return {"id": b["id"], "date": b["date"], "weekday": WEEKDAYS[d.weekday()], "time": b["time"],
                "duration_min": b["duration_min"], "client_name": b["client_name"], "phone": b["phone"],
                "car": b["car"], "work": b["work"], "note": b["note"], "status": b["status"]}

    def _warn(self, b: dict) -> dict:
        result = {"booking": self._public(b)}
        clash = self.store.overlaps(b)
        if clash:
            result["overlaps"] = [f"{x['time']} {x['client_name']} ({x['work']})" for x in clash]
        if parse_date(b["date"]) < date.today():
            result["warning"] = "дата в минулому"
        return result

    def list_bookings(self, date_from: str, date_to: str = "", query: str = "") -> str:
        """Записи в планувальнику за період (скасовані не показуються).

        Args:
            date_from: Перша дата, РРРР-ММ-ДД.
            date_to: Остання дата включно, РРРР-ММ-ДД (порожньо — лише date_from).
            query: Фільтр за клієнтом, телефоном, авто чи роботою (необов'язково).
        """
        try:
            start = parse_date(date_from).isoformat()
            end = parse_date(date_to).isoformat() if date_to else start
        except BookingError as exc:
            raise ToolError(str(exc)) from None
        items = self.store.list(start, end, query=query)
        return json.dumps([self._public(b) for b in items[:100]], ensure_ascii=False)

    def find_client(self, query: str) -> str:
        """Знайти клієнта в базі (за прізвищем, телефоном, держномером чи VIN): ПІБ, телефон, автомобілі.

        Args:
            query: Що шукати.
        """
        if self.clients is None:
            return "[]"
        q = query.lower().strip()
        digits = "".join(ch for ch in q if ch.isdigit())
        found = []
        for c in self.clients():
            cars = " ".join(f"{x.get('model', '')} {x.get('plate', '')} {x.get('vin', '')}" for x in c["cars"]).lower()
            hay = f"{c['name']} {cars}".lower()
            phone = "".join(ch for ch in c["phone"] if ch.isdigit())
            if (len(digits) >= 4 and digits in phone) or all(w in hay for w in q.split()):
                found.append({"name": c["name"], "phone": c["phone"],
                              "cars": [" ".join(x for x in (f"Tesla {car.get('model', '')}".strip(),
                                                            car.get("plate", ""), car.get("vin", "")) if x)
                                       for car in c["cars"]],
                              "last_visit": c["last_visit"]})
        return json.dumps(found[:5], ensure_ascii=False)

    def create_booking(self, date: str, time: str, client_name: str, work: str = "", phone: str = "",
                       car: str = "", duration_min: int = 60, note: str = "") -> str:
        """Створити запис у планувальнику.

        Args:
            date: Дата, РРРР-ММ-ДД.
            time: Час початку, ГГ:ХХ.
            client_name: Клієнт (як назвав співробітник або з бази).
            work: Що треба зробити, вільним текстом.
            phone: Телефон клієнта, якщо відомий.
            car: Автомобіль (модель, держномер), якщо відомий.
            duration_min: Тривалість у хвилинах (за замовчуванням 60).
            note: Примітка.
        """
        try:
            b = self.store.create({"date": date, "time": time, "client_name": client_name, "work": work,
                                   "phone": phone, "car": car, "duration_min": duration_min, "note": note},
                                  by=self.author, source=self.source)
        except BookingError as exc:
            raise ToolError(str(exc)) from None
        return json.dumps(self._warn(b), ensure_ascii=False)

    def update_booking(self, booking_id: str, date: str = "", time: str = "", duration_min: int | None = None,
                       client_name: str = "", phone: str = "", car: str = "", work: str = "", note: str = "") -> str:
        """Змінити або перенести запис. Передавайте лише поля, що змінюються.

        Args:
            booking_id: id запису (з list_bookings).
            date: Нова дата, РРРР-ММ-ДД.
            time: Новий час, ГГ:ХХ.
            duration_min: Нова тривалість, хв.
            client_name: Клієнт.
            phone: Телефон.
            car: Автомобіль.
            work: Що зробити.
            note: Примітка.
        """
        changes = {k: v for k, v in (("date", date), ("time", time), ("client_name", client_name),
                                     ("phone", phone), ("car", car), ("work", work), ("note", note)) if v}
        if duration_min:
            changes["duration_min"] = duration_min
        try:
            _, b = self.store.update(booking_id, changes, by=self.author)
        except BookingError as exc:
            raise ToolError(str(exc)) from None
        except KeyError:
            raise ToolError("Запис не знайдено") from None
        return json.dumps(self._warn(b), ensure_ascii=False)

    def cancel_booking(self, booking_id: str) -> str:
        """Скасувати запис.

        Args:
            booking_id: id запису (з list_bookings).
        """
        try:
            b = self.store.set_status(booking_id, CANCELLED, by=self.author)
        except KeyError:
            raise ToolError("Запис не знайдено") from None
        return json.dumps({"cancelled": self._public(b)}, ensure_ascii=False)

    def tool_methods(self) -> list:
        return [self.list_bookings, self.find_client, self.create_booking, self.update_booking, self.cancel_booking]


class BookingAgent:
    """Помічник запису. Тримає коротку історію розмови для кожного каналу (сайт, чат у Telegram),
    щоб можна було відповісти на уточнення: «о котрій?» — «о 10»."""

    IDLE = 30 * 60
    MAX_MESSAGES = 40

    def __init__(self, store: BookingStore, client: anthropic.Anthropic | None = None, model: str = "",
                 clients=None, service: dict | None = None, ledger=None, effort: str = "low"):
        self.store = store
        self.client = client
        self.model = model
        self.clients = clients
        self.service = service if service is not None else {}
        self.ledger = ledger
        self.effort = effort
        self._threads: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _system(self) -> str:
        now = datetime.now()
        name = self.service.get("name", "")
        return (SYSTEM_PROMPT.replace("{service}", f" {name}" if name else "")
                .replace("{today}", f"{WEEKDAYS[now.weekday()]}, {now:%Y-%m-%d}").replace("{now}", f"{now:%H:%M}"))

    def reset(self, channel: str) -> None:
        with self._lock:
            self._threads.pop(channel, None)

    def send(self, channel: str, text: str, author: str = "", source: str = "web") -> dict:
        """Обробити репліку співробітника. Повертає {"reply", "usage"}."""
        text = text.strip()
        if not text:
            raise BookingError("Порожнє повідомлення")
        with self._lock:
            thread = self._threads.get(channel)
            if thread is None or time.monotonic() - thread["used"] > self.IDLE or len(thread["messages"]) > self.MAX_MESSAGES:
                thread = self._threads[channel] = {"messages": [], "used": 0, "lock": threading.Lock()}
            thread["used"] = time.monotonic()
        if not thread["lock"].acquire(blocking=False):
            raise BookingError("Помічник ще обробляє попереднє повідомлення")
        messages = thread["messages"]
        start = len(messages)
        cost = usage_mod.empty()
        try:
            messages.append({"role": "user", "content": f"[{author or 'Співробітник'}]: {text}"})
            tools = BookingTools(self.store, self.clients, author, source)
            runner = self.client.beta.messages.tool_runner(
                model=self.model, max_tokens=4000, system=self._system(),
                tools=[beta_tool(m) for m in tools.tool_methods()], messages=list(messages),
                thinking={"type": "adaptive"}, output_config={"effort": self.effort},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            last = None
            for message in runner:
                last = message
                usage_mod.add(cost, usage_mod.from_response(message.usage, message.model))
                messages.append({"role": "assistant", "content": message.content})
                tool_response = runner.generate_tool_call_response()
                if tool_response is not None:
                    messages.append(tool_response)
        except Exception:
            del messages[start:]
            raise
        finally:
            thread["lock"].release()
            if self.ledger is not None:
                self.ledger.record("booking", cost, self.model, None)
        if last is None:
            reply = ""
        elif last.stop_reason == "refusal":
            reply = "Модель відмовилася відповідати. Переформулюйте, будь ласка."
        else:
            reply = "\n".join(b.text for b in last.content if b.type == "text").strip()
        return {"reply": reply, "usage": cost}
