"""Telegram-бот для робочої групи: картки замовлень-нарядів і внутрішні коментарі.

- Коли замовлення-наряд виходить зі статусу «чернетка», бот публікує в групу картку й оновлює
  її при змінах; про зміну статусу пише окрему відповідь під карткою.
- Відповідь (Reply) на картку чи на повідомлення бота про замовлення стає внутрішнім коментарем;
  так само повідомлення, що починається з номера: «ЗН-2026-00001 замінили колодки».
- Коментарі з веб-інтерфейсу бот публікує відповіддю під карткою.
- Фото, надіслане відповіддю на картку (чи з номером у підписі), додається до фото замовлення.
- Команди: /orders — замовлення в роботі, /order <номер> — картка з коментарями, /chatid — id чату.
Бот працює лише з однією групою (TELEGRAM_CHAT_ID); повідомлення з інших чатів ігноруються.

Клієнтам (в особистий чат, якщо клієнт підключився за посиланням-запрошенням) бот надсилає
замовлення-наряд у PDF і вибрані фото, кошторис на погодження з кнопками «Погоджую / Відмовляюсь»
і повідомлення, коли автомобіль готовий. Власнику (BACKUP_CHAT_ID) щоночі надсилає резервну копію.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal
from html import escape
from pathlib import Path

import httpx

from . import bookings as bk
from .clients import phone_key as _phone_key
from .comments import CommentError, CommentStore
from .order import DRAFT, STATUSES, OrderStore, RepairOrder, StatusError, now_str

try:
    from zoneinfo import ZoneInfo

    KYIV = ZoneInfo("Europe/Kyiv")
except Exception:  # немає бази часових поясів — працюємо за часом сервера
    KYIV = None

log = logging.getLogger(__name__)

ACTIVE = ("прийнято", "в роботі", "готово")
STATUS_ICON = {"прийнято": "📥", "в роботі": "🔧", "готово": "✅", "видано": "🏁", "скасовано": "❌"}
NUMBER_IN_TEXT = re.compile(r"^\s*(ЗН-\d{4}-\d{5})[\s:,.\-–—]+(.+)$", re.S)
HELP = ("Я публікую сюди замовлення-наряди й збираю внутрішні коментарі.\n\n"
        "• Відповідайте (Reply) на картку замовлення — відповідь стане коментарем.\n"
        "• Або почніть повідомлення з номера: <code>ЗН-2026-00001 замінили колодки</code>.\n"
        "• Фото відповіддю на картку додається до фото замовлення (підпис «до», «після» чи "
        "«пошкодження» задає тип).\n"
        "• Голосове відповіддю на картку — голосовий коментар (прослухати можна й на сайті).\n"
        "• Кнопки під карткою: змінити статус і «👤 Беру собі» — стати відповідальним.\n"
        "• /orders — замовлення в роботі\n• /my — мої замовлення\n"
        "• /order ЗН-2026-00001 — картка з коментарями\n"
        "• /zapys Петренко пт 10:00 ТО — записати клієнта (вільним текстом); /zapys — записи на сьогодні й завтра. "
        "Відповідь на повідомлення про запис («перенеси на 12») — змінити його.")
NEXT_STATUS = {"прийнято": ("в роботі", "🔧 В роботу"), "в роботі": ("готово", "✅ Готово"),
               "готово": ("видано", "🏁 Видано")}
SCHEDULE_HOUR = 8  # о котрій (за Києвом) надсилати в групу записи на сьогодні
SUMMARY_HOUR = 20  # о котрій (за Києвом) надсилати власнику підсумок дня
STALE_DAYS = 2  # «довго в роботі» — понад стільки днів
READY = "готово"
BACKUP_HOUR = 3  # о котрій (за Києвом) щоночі надсилати резервну копію
APPROVE, DECLINE = "y", "n"


class TelegramError(Exception):
    pass


class ClientNotConnected(Exception):
    pass


class TelegramAPI:
    def __init__(self, token: str, http: httpx.Client | None = None):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.file_base = f"https://api.telegram.org/file/bot{token}/"
        self.http = http or httpx.Client(timeout=45)

    def call(self, method: str, **params):
        response = self.http.post(self.base + method, json={k: v for k, v in params.items() if v is not None})
        return self._result(method, response)

    def send_document(self, chat_id: int, filename: str, data: bytes, caption: str = "",
                      parse_mode: str | None = None, content_type: str = "application/pdf",
                      reply_markup: dict | None = None):
        fields = {"chat_id": str(chat_id), "caption": caption}
        if parse_mode:
            fields["parse_mode"] = parse_mode
        if reply_markup:
            fields["reply_markup"] = json.dumps(reply_markup)
        response = self.http.post(self.base + "sendDocument", data=fields,
                                  files={"document": (filename, data, content_type)}, timeout=120)
        return self._result("sendDocument", response)

    def send_photos(self, chat_id: int, photos: list[tuple[str, bytes, str]], caption: str = ""):
        """Надіслати фото альбомами по 10 (photos: [(ім'я файлу, дані, тип)])."""
        for start in range(0, len(photos), 10):
            chunk = photos[start:start + 10]
            if len(chunk) == 1:
                name, data, mime = chunk[0]
                response = self.http.post(self.base + "sendPhoto", data={"chat_id": str(chat_id), "caption": caption},
                                          files={"photo": (name, data, mime)}, timeout=120)
                self._result("sendPhoto", response)
            else:
                media = [{"type": "photo", "media": f"attach://p{i}", **({"caption": caption} if i == 0 else {})}
                         for i in range(len(chunk))]
                files = {f"p{i}": photo for i, photo in enumerate(chunk)}
                response = self.http.post(self.base + "sendMediaGroup", files=files, timeout=120,
                                          data={"chat_id": str(chat_id), "media": json.dumps(media, ensure_ascii=False)})
                self._result("sendMediaGroup", response)
            caption = ""

    def download(self, file_id: str) -> bytes:
        path = self.call("getFile", file_id=file_id)["file_path"]
        response = self.http.get(self.file_base + path, timeout=60)
        if response.status_code != 200:
            raise TelegramError(f"getFile: HTTP {response.status_code}")
        return response.content

    @staticmethod
    def _result(method: str, response):
        try:
            data = response.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {response.status_code}") from None
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', 'помилка')}")
        return data["result"]


def money(value) -> str:
    return f"{float(value):,.2f}".replace(",", " ").replace(".", ",")


def short_list(items: list[str], limit: int = 6) -> str:
    shown = [escape(x) for x in items[:limit]]
    if len(items) > limit:
        shown.append(f"… ще {len(items) - limit}")
    return "; ".join(shown)


class TelegramBot:
    def __init__(self, api: TelegramAPI, chat_id: int, store: OrderStore, comments: CommentStore,
                 links_path: str | Path):
        self.api = api
        self.chat_id = int(chat_id)
        self.store = store
        self.comments = comments
        self.links_path = Path(links_path)
        self._lock = threading.RLock()
        self._dirty: set[str] = set()  # замовлення, картки яких треба оновити
        self._tasks: list[tuple] = []  # інші вихідні повідомлення по черзі
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.links = self._load_links()
        self.render_pdf = None  # order -> bytes; задає web.py (друкована форма у PDF)
        self.render_act = None  # order -> bytes: акт приймання у PDF; задає web.py
        self.service_name = ""
        self.service_address = ""
        self.photos = None  # PhotoStore; задає web.py
        self.backup_chat_id = 0  # особистий чат власника для резервних копій (BACKUP_CHAT_ID)
        self.make_backup = None  # () -> (дані zip, info, ім'я файлу); задає web.py
        self.on_order_changed = None  # order -> None: зміна з Telegram (статус, відповідальний); задає web.py
        self.stock = None  # Stock; задає web.py
        self.stock_parts = None  # () -> довідник запчастин
        self.bookings = None  # BookingStore; задає web.py
        self.booking_agent = None  # BookingAgent; задає web.py
        self._username: str | None = None

    # --- зв'язок повідомлень із замовленнями (файл на диску) ---

    def _load_links(self) -> dict:
        try:
            data = json.loads(self.links_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            data = {}
        return {"orders": data.get("orders", {}), "messages": data.get("messages", {}), "offset": data.get("offset", 0),
                # Клієнти: посилання-запрошення до бота й чати тих, хто натиснув «Старт».
                "client_tokens": data.get("client_tokens", {}), "order_tokens": data.get("order_tokens", {}),
                "clients": data.get("clients", {}),
                # Кошториси на погодженні: номер -> {status, total, chat_id, message_id, sent_at, at}.
                "approvals": data.get("approvals", {}),
                "ready_sent": data.get("ready_sent", {}),  # кому вже написали «автомобіль готовий»
                "backup_date": data.get("backup_date", ""),
                "summary_date": data.get("summary_date", ""),
                "schedule_date": data.get("schedule_date", ""),
                "bookings": data.get("bookings", {}),  # id запису -> {message_id, text}
                "booking_msgs": data.get("booking_msgs", {}),  # message_id -> id запису
                "broadcasts": data.get("broadcasts", [])}  # журнал розсилок

    def _save_links(self) -> None:
        self.links_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.links_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.links, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.links_path)

    def _remember(self, message_id: int, number: str) -> None:
        with self._lock:
            self.links["messages"][str(message_id)] = number

    def number_for_message(self, message_id: int) -> str | None:
        return self.links["messages"].get(str(message_id))

    # --- події з сервісу (швидкі: лише ставлять у чергу) ---

    def order_saved(self, order: RepairOrder) -> None:
        # Без групи теж: клієнту треба повідомити, що автомобіль готовий.
        with self._lock:
            self._dirty.add(order.number)
        self._wake.set()

    def comment_added(self, number: str, comment: dict) -> None:
        if not self.chat_id:
            return  # група ще не налаштована
        with self._lock:
            self._tasks.append(("comment", number, comment))
            self._dirty.add(number)
        self._wake.set()

    def booking_changed(self, kind: str, booking: dict) -> None:
        with self._lock:
            self._tasks.append(("booking", booking["id"], (kind, booking)))
        self._wake.set()

    def stock_low(self, items: list[dict]) -> None:
        """Залишок запчастин опустився до мінімуму — написати в групу."""
        if not self.chat_id or not items:
            return
        with self._lock:
            self._tasks.append(("stock", "", items))
        self._wake.set()

    def order_deleted(self, number: str) -> None:
        if not self.chat_id:
            return  # група ще не налаштована
        with self._lock:
            self._tasks.append(("deleted", number, None))
        self._wake.set()

    # --- надсилання ---

    def card(self, order: RepairOrder) -> str:
        v, c, t = order.vehicle, order.customer, order.totals()
        car = " · ".join(x for x in (
            f"Tesla {v.model} {v.year or ''}".strip() if v.model else "",
            v.plate, f"{v.odometer_km:,} км".replace(",", " ") if v.odometer_km is not None else "") if x)
        lines = [f"🚗 <b>{escape(order.number)}</b> · {STATUS_ICON.get(order.status, '')} <b>{escape(order.status)}</b>"]
        if car:
            lines.append(escape(car))
        if c.name or c.phone:
            lines.append("👤 " + escape(" · ".join(x for x in (c.name, c.phone) if x)))
        if order.complaints:
            lines.append("⚠️ Скарги: " + short_list(order.complaints))
        if order.labor:
            lines.append("🔧 Роботи: " + short_list([line.name for line in order.labor]))
        if order.parts:
            lines.append("📦 Запчастини: " + short_list([line.name for line in order.parts]))
        lines.append(f"💵 Разом: {money(t['total'])} {escape(order.currency)}")
        if order.assignee:
            lines.append(f"👤 Відповідальний: {escape(order.assignee)}")
        count = self.comments.count(order.number)
        if count:
            lines.append(f"💬 Коментарів: {count}")
        lines.append("<i>Відповідайте на це повідомлення, щоб залишити коментар.</i>")
        return "\n".join(lines)[:4000]

    @staticmethod
    def card_markup(order: RepairOrder) -> dict:
        """Кнопки під карткою: наступний статус і «Беру собі» (для замовлень у роботі)."""
        row = []
        if order.status in NEXT_STATUS:
            status, label = NEXT_STATUS[order.status]
            row.append({"text": label, "callback_data": f"st:{order.number}:{STATUSES.index(status)}"})
        if order.status in ACTIVE:
            row.append({"text": "👤 Беру собі", "callback_data": f"me:{order.number}"})
        return {"inline_keyboard": [row] if row else []}

    def _send(self, text: str, reply_to: int | None = None, reply_markup: dict | None = None) -> int:
        result = self.api.call("sendMessage", chat_id=self.chat_id, text=text, parse_mode="HTML",
                               reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True}
                               if reply_to else None, reply_markup=reply_markup)
        return result["message_id"]

    def _sync_order(self, number: str) -> None:
        try:
            order = self.store.load(number)
        except (FileNotFoundError, ValueError):
            return
        self._notify_ready(order)
        if not self.chat_id:
            return  # група ще не налаштована
        link = self.links["orders"].get(number)
        if link is None and order.status == DRAFT:
            return  # чернетки в групу не потрапляють
        text, markup = self.card(order), self.card_markup(order)
        if link is None:
            message_id = self._send(text, reply_markup=markup)
            self.links["orders"][number] = {"message_id": message_id, "status": order.status, "text": text,
                                            "markup": markup, "assignee": order.assignee}
            self._remember(message_id, number)
            return
        if text != link["text"] or markup != link.get("markup"):
            try:
                self.api.call("editMessageText", chat_id=self.chat_id, message_id=link["message_id"], text=text,
                              parse_mode="HTML", reply_markup=markup)
            except TelegramError as exc:
                if "not modified" not in str(exc):
                    raise
            link["text"], link["markup"] = text, markup
        if order.status != link["status"]:
            icon = STATUS_ICON.get(order.status, "📌")
            last = order.status_log[-1] if order.status_log else {}
            by = f" · {escape(last['by'])}" if last.get("status") == order.status and last.get("by") else ""
            debt = order.balance()
            warn = f" · ⚠️ борг {money(debt)} {escape(order.currency)}" if order.status == "видано" and debt > 0 else ""
            reply = self._send(f"{icon} {escape(number)}: статус «<b>{escape(order.status)}</b>»{by}{warn}",
                               link["message_id"])
            self._remember(reply, number)
            link["status"] = order.status
        if order.assignee != link.get("assignee", ""):
            if order.assignee:
                self._remember(self._send(f"👤 {escape(number)}: відповідальний — <b>{escape(order.assignee)}</b>",
                                          link["message_id"]), number)
            link["assignee"] = order.assignee

    def _run_task(self, kind: str, number: str, payload) -> None:
        if kind == "booking":
            self._sync_booking(*payload)
            return
        if kind == "stock":
            rows = [f"• {escape(i['name'])} ({escape(i['part_no'])}) — залишилось <b>{i['qty']:g} {escape(i['unit'])}</b>"
                    f" (мін. {i['min']:g})" for i in payload]
            self._send("📦 <b>Закінчується на складі:</b>\n" + "\n".join(rows))
            return
        link = self.links["orders"].get(number)
        if kind == "comment" and link:
            text = f"💬 <b>{escape(payload['author'])}</b>: {escape(payload['text'])}"
            self._remember(self._send(text, link["message_id"]), number)
        elif kind == "deleted" and link:
            self._send(f"🗑 {escape(number)}: замовлення-наряд видалено", link["message_id"])

    def flush(self) -> None:
        """Надіслати все, що накопичилось. Помилки логуються; невдале повториться наступного разу."""
        with self._lock:
            tasks, self._tasks = self._tasks, []
            dirty, self._dirty = self._dirty, set()
        failed_tasks, failed_dirty = [], set()
        # Під замком: таблицю зв'язків змінює й потік, що приймає повідомлення.
        with self._lock:
            # Спершу картки: коментар, написаний одразу після прийняття замовлення, має куди «прикріпитися».
            for number in sorted(dirty):
                try:
                    self._sync_order(number)
                except (TelegramError, httpx.HTTPError):
                    log.exception("Telegram: не вдалося оновити картку %s", number)
                    failed_dirty.add(number)
            for task in tasks:
                if task[1] in failed_dirty:
                    failed_tasks.append(task)  # картка не оновилась — коментар чекає разом із нею
                    continue
                try:
                    self._run_task(*task)
                except (TelegramError, httpx.HTTPError):
                    log.exception("Telegram: не вдалося надіслати %s", task[:2])
                    failed_tasks.append(task)
            self._save_links()
            self._tasks[:0] = failed_tasks
            self._dirty |= failed_dirty

    # --- вхідні повідомлення ---

    @staticmethod
    def author(sender: dict) -> str:
        name = " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x) or "Співробітник"
        return f"{name} (@{sender['username']})" if sender.get("username") else name

    def handle_update(self, update: dict) -> None:
        if update.get("callback_query"):
            self._callback(update["callback_query"])
            return
        msg = update.get("message")
        if not msg or msg.get("from", {}).get("is_bot"):
            return
        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or msg.get("caption") or "").strip()
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if command == "/chatid":  # для налаштування: дізнатися id групи
            self.api.call("sendMessage", chat_id=chat_id, text=f"ID цього чату: {chat_id}")
            return
        if msg["chat"].get("type") == "private":
            self._private(msg, text, command)
            return
        if chat_id != self.chat_id:
            return  # чужі чати ігноруємо
        if command in ("/start", "/help"):
            self.api.call("sendMessage", chat_id=chat_id, text=HELP, parse_mode="HTML")
        elif command == "/orders":
            self._list_active()
        elif command == "/order":
            self._show_order(text.split(maxsplit=1)[1] if " " in text else "")
        elif command == "/my":
            self._list_mine(msg.get("from", {}))
        elif command in ("/zapys", "/zapis", "/booking"):
            rest = text.split(maxsplit=1)[1].strip() if " " in text else ""
            if rest:
                self._booking_command(msg, rest)
            else:
                self.api.call("sendMessage", chat_id=self.chat_id, text=self.schedule_text(2), parse_mode="HTML")
        elif text and not command and msg.get("reply_to_message") \
                and str(msg["reply_to_message"]["message_id"]) in self.links["booking_msgs"]:
            self._booking_command(msg, text, self.links["booking_msgs"][str(msg["reply_to_message"]["message_id"])])
        elif (msg.get("voice") or msg.get("audio")) and not command:
            self._voice_from(msg, text)
        elif msg.get("photo") and not command:
            self._photo_from(msg, text)
        elif text and not command:
            self._comment_from(msg, text)

    def _order_for(self, msg: dict, text: str) -> tuple[str | None, str]:
        """До якого замовлення належить повідомлення групи: відповідь на картку або номер на початку."""
        reply = msg.get("reply_to_message")
        number = self.number_for_message(reply["message_id"]) if reply else None
        if number is None:
            match = NUMBER_IN_TEXT.match(text) or re.match(r"^\s*(ЗН-\d{4}-\d{5})()\s*$", text)
            if not match:
                return None, text  # звичайна розмова в групі — не до замовлення
            number, text = match.group(1), match.group(2).strip()
        try:
            self.store.load(number)
        except FileNotFoundError:
            self.api.call("sendMessage", chat_id=self.chat_id, text=f"Замовлення-наряд {number} не знайдено",
                          reply_parameters={"message_id": msg["message_id"]})
            return None, text
        return number, text

    def _react(self, message_id: int) -> None:
        try:
            self.api.call("setMessageReaction", chat_id=self.chat_id, message_id=message_id,
                          reaction=[{"type": "emoji", "emoji": "👍"}])
        except TelegramError:
            pass  # реакції можуть бути вимкнені в групі — дані однаково збережено

    def _photo_from(self, msg: dict, text: str) -> None:
        number, text = self._order_for(msg, text)
        if number is None or self.photos is None:
            return
        from .photos import KINDS, PhotoError

        words = text.lower().split()
        kind = next((k for k in KINDS if k in words), "інше")
        biggest = max(msg["photo"], key=lambda p: p.get("file_size") or p.get("width", 0) * p.get("height", 0))
        try:
            self.photos.add(number, self.api.download(biggest["file_id"]), "image/jpeg", kind,
                            self.author(msg.get("from", {})), source="telegram")
        except PhotoError as exc:
            self.api.call("sendMessage", chat_id=self.chat_id, text=str(exc),
                          reply_parameters={"message_id": msg["message_id"]})
            return
        self._remember(msg["message_id"], number)
        self._react(msg["message_id"])

    def _voice_from(self, msg: dict, text: str) -> None:
        number, _ = self._order_for(msg, text)
        if number is None:
            return
        media = msg.get("voice") or msg.get("audio")
        ext = {"audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/x-m4a": "m4a"}.get(media.get("mime_type", ""), "ogg")
        duration = int(media.get("duration") or 0)
        try:
            name = self.comments.save_voice(number, self.api.download(media["file_id"]), ext)
            self.comments.add(number, self.author(msg.get("from", {})),
                              f"🎤 Голосове повідомлення {duration // 60}:{duration % 60:02d}" + (f" — {text}" if text else ""),
                              source="telegram", voice={"file": name, "duration": duration})
        except CommentError as exc:
            self.api.call("sendMessage", chat_id=self.chat_id, text=str(exc),
                          reply_parameters={"message_id": msg["message_id"]})
            return
        self._remember(msg["message_id"], number)
        with self._lock:
            self._dirty.add(number)
        self._react(msg["message_id"])
        self._wake.set()

    def _comment_from(self, msg: dict, text: str) -> None:
        number, text = self._order_for(msg, text)
        if number is None or not text:
            return
        try:
            self.comments.add(number, self.author(msg.get("from", {})), text, source="telegram")
        except CommentError as exc:
            self.api.call("sendMessage", chat_id=self.chat_id, text=str(exc),
                          reply_parameters={"message_id": msg["message_id"]})
            return
        self._remember(msg["message_id"], number)  # відповіді на цей коментар — теж до цього замовлення
        with self._lock:
            self._dirty.add(number)  # оновити лічильник коментарів у картці
        self._react(msg["message_id"])
        self._wake.set()

    @staticmethod
    def _order_row(o: RepairOrder) -> str:
        return (f"{STATUS_ICON[o.status]} <b>{escape(o.number)}</b> · {escape(o.status)} · "
                f"{escape(' '.join(x for x in (o.vehicle.model, o.vehicle.plate) if x) or '—')} · "
                f"{escape(o.customer.name or '—')}" + (f" · 👤 {escape(o.assignee)}" if o.assignee else ""))

    def _list_active(self) -> None:
        active = [o for o in self.store.list() if o.status in ACTIVE]
        if not active:
            text = "Зараз немає замовлень-нарядів у роботі."
        else:
            text = "Замовлення-наряди в роботі:\n" + "\n".join(self._order_row(o) for o in active)
        self.api.call("sendMessage", chat_id=self.chat_id, text=text[:4000], parse_mode="HTML")

    def _is_mine(self, order: RepairOrder, sender: dict) -> bool:
        if order.assignee_tg is not None:
            return order.assignee_tg == sender.get("id")
        return bool(order.assignee) and order.assignee == self.author(sender)

    def _list_mine(self, sender: dict) -> None:
        mine = [o for o in self.store.list() if o.status in ACTIVE and self._is_mine(o, sender)]
        name = escape(self.author(sender))
        text = (f"Замовлення-наряди, де {name} — відповідальний:\n" + "\n".join(self._order_row(o) for o in mine)
                if mine else f"{name}: зараз за вами немає замовлень у роботі. Натисніть «👤 Беру собі» під карткою.")
        self.api.call("sendMessage", chat_id=self.chat_id, text=text[:4000], parse_mode="HTML")

    # --- запис на сервіс ---

    @staticmethod
    def booking_text(b: dict) -> str:
        head = f"📅 <b>Запис</b> · {escape(bk.label(b))} ({b['duration_min']} хв)"
        lines = [head, "👤 " + escape(" · ".join(x for x in (b["client_name"], b["phone"]) if x))]
        if b["car"]:
            lines.append(f"🚗 {escape(b['car'])}")
        if b["work"]:
            lines.append(f"🔧 {escape(b['work'])}")
        if b["note"]:
            lines.append(f"📝 {escape(b['note'])}")
        if b["status"] == bk.CANCELLED:
            lines = [f"❌ <s>{line}</s>" if i == 0 else f"<s>{line}</s>" for i, line in enumerate(lines)]
            lines.append("<b>Скасовано</b>")
        elif b["status"] != bk.PLANNED:
            lines.append(f"<b>{escape(b['status'])}</b>" + (f" · {escape(b['order_number'])}" if b.get("order_number") else ""))
        if b.get("created_by"):
            lines.append(f"<i>записав(-ла) {escape(b['created_by'])}</i>")
        return "\n".join(lines)[:4000]

    def _sync_booking(self, kind: str, b: dict) -> None:
        before = b.pop("_before", None)
        moved = before is not None and (before["date"], before["time"]) != (b["date"], b["time"])
        if self.chat_id:
            text = self.booking_text(b)
            link = self.links["bookings"].get(b["id"])
            if link is None:
                if kind == "created":
                    message_id = self._send(text)
                    self.links["bookings"][b["id"]] = {"message_id": message_id, "text": text}
                    self.links["booking_msgs"][str(message_id)] = b["id"]
            else:
                if text != link["text"]:
                    try:
                        self.api.call("editMessageText", chat_id=self.chat_id, message_id=link["message_id"],
                                      text=text, parse_mode="HTML")
                    except TelegramError as exc:
                        if "not modified" not in str(exc):
                            raise
                    link["text"] = text
                note = None
                if moved:
                    note = f"🔁 Перенесено: {escape(bk.label(before))} → <b>{escape(bk.label(b))}</b>"
                elif kind == "cancelled":
                    note = f"❌ Запис {escape(bk.label(b))} · {escape(b['client_name'])} скасовано"
                if note:
                    reply = self._send(note, link["message_id"])
                    self.links["booking_msgs"][str(reply)] = b["id"]
        self._notify_client_booking(kind, b, moved)

    def _notify_client_booking(self, kind: str, b: dict, moved: bool) -> None:
        key = self.phone_key(b.get("phone", ""))
        client = self.links["clients"].get(key) if key else None
        if not client or client.get("blocked"):
            return
        place = ", ".join(x for x in (self.service_name, self.service_address) if x)
        if kind == "created" and b["status"] == bk.PLANNED:
            text = f"📅 Вас записано{(' до ' + self.service_name) if self.service_name else ''}: {bk.long_label(b)}."
            if self.service_address:
                text += f"\nАдреса: {self.service_address}."
        elif moved and b["status"] == bk.PLANNED:
            text = f"🔁 Ваш запис перенесено на {bk.long_label(b)}." + (f"\n{place}" if place else "")
        elif kind == "cancelled":
            text = f"Ваш запис на {bk.long_label(b)} скасовано. Якщо потрібно — запишемо на інший час."
        else:
            return
        try:
            self.api.call("sendMessage", chat_id=client["chat_id"], text=text)
        except TelegramError:
            log.exception("Telegram: не вдалося повідомити клієнта про запис")

    def schedule_text(self, days: int = 1, start: datetime | None = None) -> str:
        start = (start or datetime.now()).date()
        parts = []
        for i in range(days):
            day = start + timedelta(days=i)
            items = self.bookings.list(day.isoformat(), day.isoformat()) if self.bookings else []
            today = datetime.now().date()
            title = {today: "Сьогодні, ", today + timedelta(days=1): "Завтра, "}.get(day, "")
            head = f"📅 <b>{title}{bk.WEEKDAYS[day.weekday()]} {day:%d.%m}</b>"
            if not items:
                parts.append(f"{head}\nзаписів немає")
                continue
            rows = [f"• <b>{escape(b['time'])}</b> {escape(b['client_name'])}"
                    + (f" · {escape(b['car'])}" if b["car"] else "") + (f" — {escape(b['work'])}" if b["work"] else "")
                    + ("" if b["status"] == bk.PLANNED else f" ({escape(b['status'])})") for b in items]
            parts.append(head + "\n" + "\n".join(rows))
        return "\n\n".join(parts)[:4000]

    def _booking_command(self, msg: dict, text: str, booking_id: str | None = None) -> None:
        if self.booking_agent is None:
            return
        if booking_id:
            try:
                b = self.bookings.get(booking_id)
                text = f"(Йдеться про запис id={b['id']}: {bk.label(b)}, {b['client_name']}) {text}"
            except KeyError:
                pass
        sender = msg.get("from", {})
        try:
            result = self.booking_agent.send(f"tg:{self.chat_id}:{sender.get('id')}", text, self.author(sender),
                                             source="telegram")
            reply = result["reply"] or "Готово."
        except bk.BookingError as exc:
            reply = str(exc)
        except Exception:
            log.exception("Помічник запису")
            reply = "Не вдалося обробити запис — спробуйте ще раз або запишіть на сайті."
        self.api.call("sendMessage", chat_id=self.chat_id, text=reply[:4000],
                      reply_parameters={"message_id": msg["message_id"], "allow_sending_without_reply": True})
        self._wake.set()

    def schedule_tick(self) -> None:
        now = datetime.now()
        if not self.chat_id or self.bookings is None or now.hour < SCHEDULE_HOUR \
                or self.links["schedule_date"] == now.strftime("%Y-%m-%d"):
            return
        if self.bookings.list(now.date().isoformat(), now.date().isoformat()):
            self._send(self.schedule_text(1))
        with self._lock:
            self.links["schedule_date"] = now.strftime("%Y-%m-%d")
            self._save_links()

    # --- кнопки під карткою в групі ---

    def _staff_callback(self, query: dict, parts: list[str]) -> None:
        msg = query.get("message") or {}
        if msg.get("chat", {}).get("id") != self.chat_id or not self.chat_id:
            self.api.call("answerCallbackQuery", callback_query_id=query["id"])
            return
        sender = query.get("from", {})
        who = self.author(sender)
        number = parts[1]
        try:
            order = self.store.load(number)
        except FileNotFoundError:
            self.api.call("answerCallbackQuery", callback_query_id=query["id"],
                          text="Замовлення-наряд не знайдено (можливо, видалено)", show_alert=True)
            return
        if parts[0] == "st":
            target = STATUSES[int(parts[2])] if len(parts) == 3 and parts[2].isdigit() and int(parts[2]) < len(STATUSES) else ""
            if not target or order.status == target:
                answer = f"Статус уже «{order.status}»"
            else:
                try:
                    order.set_status(target, by=who)
                except StatusError as exc:
                    self.api.call("answerCallbackQuery", callback_query_id=query["id"], text=str(exc)[:190],
                                  show_alert=True)
                    return
                answer = f"Статус: {target}"
        else:  # me — стати відповідальним
            if order.assignee_tg == sender.get("id") and order.assignee:
                answer = "Ви вже відповідальний за це замовлення"
                order = None
            else:
                order.assignee, order.assignee_tg = who, sender.get("id")
                answer = f"Ви відповідальний за {number}"
        if order is not None:
            self.store.save(order)  # обробник збереження оновить картку й напише в групу
            if self.on_order_changed:
                try:
                    self.on_order_changed(order)
                except Exception:
                    log.exception("Помилка обробника зміни замовлення")
        self.api.call("answerCallbackQuery", callback_query_id=query["id"], text=answer)

    def _show_order(self, number: str) -> None:
        number = number.strip().upper()
        try:
            order = self.store.load(number)
        except FileNotFoundError:
            self.api.call("sendMessage", chat_id=self.chat_id, text="Вкажіть номер: /order ЗН-2026-00001")
            return
        parts = [self.card(order)]
        comments = self.comments.list(number)
        if comments:
            parts.append("\n<b>Коментарі:</b>")
            parts += [f"• {escape(c['at'])} <b>{escape(c['author'])}</b>: {escape(c['text'])}" for c in comments[-30:]]
        self._remember(self._send("\n".join(parts)[:4000]), number)

    # --- клієнти: надсилання замовлення-наряду в особистий чат ---

    @staticmethod
    def phone_key(phone: str) -> str | None:
        """Останні 9 цифр телефону: «+380 67 123 45 67» і «067 123 45 67» — той самий клієнт."""
        return _phone_key(phone)

    def bot_username(self) -> str:
        if self._username is None:
            self._username = self.api.call("getMe")["username"]
        return self._username

    def client_link(self, number: str) -> str:
        """Посилання t.me/<бот>?start=<токен>: клієнт натискає «Старт» і отримує цей замовлення-наряд."""
        with self._lock:
            token = self.links["order_tokens"].get(number)
            if token is None:
                token = "o_" + secrets.token_urlsafe(12)  # не вгадати: у замовленні персональні дані
                self.links["order_tokens"][number] = token
                self.links["client_tokens"][token] = number
                self._save_links()
        return f"https://t.me/{self.bot_username()}?start={token}"

    def client_for(self, order: RepairOrder) -> dict | None:
        key = self.phone_key(order.customer.phone)
        return self.links["clients"].get(key) if key else None

    def _client_or_raise(self, order: RepairOrder) -> dict:
        client = self.client_for(order)
        if client is None:
            raise ClientNotConnected("Клієнт ще не підключений до бота")
        return client

    def send_to_client(self, order: RepairOrder) -> dict:
        """Надіслати PDF (і фото, позначені «клієнту») клієнту, який уже підключився до бота."""
        client = self._client_or_raise(order)
        self._send_order_document(client["chat_id"], order)
        if order.intake.get("signature") and self.render_act:
            self.api.send_document(client["chat_id"], f"Акт приймання {order.number}.pdf", self.render_act(order),
                                   f"Акт приймання автомобіля до замовлення-наряду № {order.number}")
        sent = self._send_client_photos(client["chat_id"], order.number)
        note = f"📤 {escape(order.number)}: надіслано клієнту в Telegram" + (f" (фото: {sent})" if sent else "")
        self._note_in_group(order.number, note)
        return {**client, "photos": sent}

    def _send_client_photos(self, chat_id: int, number: str) -> int:
        if self.photos is None:
            return 0
        from .photos import EXTENSIONS

        mime = {ext: m for m, ext in EXTENSIONS.items()}
        files = [(path.name, path.read_bytes(), mime.get(path.suffix, "image/jpeg"))
                 for _, path in self.photos.for_client(number) if path.exists()]
        if files:
            self.api.send_photos(chat_id, files, f"Фото до замовлення-наряду № {number}")
        return len(files)

    def _send_order_document(self, chat_id: int, order: RepairOrder, caption: str | None = None,
                             reply_markup: dict | None = None) -> int | None:
        if caption is None:
            caption = (f"Замовлення-наряд № {order.number}" + (f" · {self.service_name}" if self.service_name else "")
                       + self._to_pay(order))
        if self.render_pdf:
            result = self.api.send_document(chat_id, f"Замовлення-наряд {order.number}.pdf", self.render_pdf(order),
                                            caption, reply_markup=reply_markup)
        else:
            result = self.api.call("sendMessage", chat_id=chat_id, text=caption, reply_markup=reply_markup)
        return result.get("message_id") if isinstance(result, dict) else None

    @staticmethod
    def _to_pay(order: RepairOrder) -> str:
        """«Разом до сплати» з урахуванням передоплати."""
        cur, paid, balance = order.currency, order.paid(), order.balance()
        if paid <= 0:
            return f"\nРазом до сплати: {money(order.totals()['total'])} {cur}"
        if balance <= 0:
            return f"\nСума: {money(order.totals()['total'])} {cur}, сплачено повністю"
        return f"\nСума: {money(order.totals()['total'])} {cur}, сплачено {money(paid)} {cur}, до сплати: {money(balance)} {cur}"

    # --- погодження кошторису ---

    def approval(self, number: str) -> dict | None:
        return self.links["approvals"].get(number)

    def send_for_approval(self, order: RepairOrder) -> dict:
        """Надіслати клієнту кошторис (PDF) із кнопками «Погоджую / Відмовляюсь»."""
        client = self._client_or_raise(order)
        total = order.totals()["total"]
        caption = (f"Кошторис № {order.number}" + (f" · {self.service_name}" if self.service_name else "")
                   + f"\nСума: {money(total)} {order.currency}\n\nПогоджуєте виконання робіт?")
        keyboard = {"inline_keyboard": [[
            {"text": "✅ Погоджую", "callback_data": f"ap:{order.number}:{APPROVE}"},
            {"text": "❌ Відмовляюсь", "callback_data": f"ap:{order.number}:{DECLINE}"}]]}
        message_id = self._send_order_document(client["chat_id"], order, caption, keyboard)
        approval = {"status": "pending", "total": str(total), "currency": order.currency,
                    "chat_id": client["chat_id"], "message_id": message_id, "sent_at": now_str(), "at": ""}
        with self._lock:
            self.links["approvals"][order.number] = approval
            self._save_links()
        self._send_client_photos(client["chat_id"], order.number)
        self._note_in_group(order.number, f"📝 {escape(order.number)}: кошторис на {money(total)} "
                                          f"{escape(order.currency)} надіслано клієнту на погодження")
        return approval

    def _callback(self, query: dict) -> None:
        data = query.get("data") or ""
        msg = query.get("message") or {}
        parts = data.split(":")
        if parts[0] in ("st", "me") and len(parts) >= 2:
            self._staff_callback(query, parts)
            return
        if len(parts) != 3 or parts[0] != "ap":
            self.api.call("answerCallbackQuery", callback_query_id=query["id"])
            return
        number, answer = parts[1], parts[2]
        chat_id = msg.get("chat", {}).get("id")
        with self._lock:
            approval = self.links["approvals"].get(number)
            valid = (approval is not None and approval["chat_id"] == chat_id
                     and approval.get("message_id") in (None, msg.get("message_id")))
            if not valid:
                reply = "Цей кошторис застарів. Чекайте на новий від майстра."
            elif approval["status"] != "pending":
                reply = "Ви вже відповіли на цей кошторис."
            else:
                approval["status"] = "approved" if answer == APPROVE else "declined"
                approval["at"] = now_str()
                self._save_links()
                reply = None
        if reply:
            self.api.call("answerCallbackQuery", callback_query_id=query["id"], text=reply, show_alert=True)
            return
        approved = approval["status"] == "approved"
        self.api.call("answerCallbackQuery", callback_query_id=query["id"],
                      text="Дякуємо! Роботи погоджено." if approved else "Зрозуміли, роботи не виконуватимемо.")
        try:  # прибрати кнопки, щоб не натиснули вдруге
            self.api.call("editMessageReplyMarkup", chat_id=chat_id, message_id=msg.get("message_id"),
                          reply_markup={"inline_keyboard": []})
        except TelegramError:
            pass
        sum_text = f"{money(approval['total'])} {approval.get('currency', '$')}"
        self.api.call("sendMessage", chat_id=chat_id, text=(
            f"✅ Дякуємо! Кошторис № {number} на {sum_text} погоджено. Ми беремося до роботи." if approved else
            f"Зрозуміли, роботи за кошторисом № {number} не виконуватимемо. Майстер зв'яжеться з вами."))
        verdict = "погодив" if approved else "відхилив"
        client = self.author(query.get("from", {}))
        try:
            self.comments.add(number, f"Клієнт: {client}", f"{'✅' if approved else '❌'} Клієнт {verdict} кошторис "
                              f"на {sum_text} (Telegram)", source="telegram")
        except (CommentError, FileNotFoundError):
            log.exception("Не вдалося записати погодження в коментарі")
        self._note_in_group(number, f"{'✅' if approved else '❌'} {escape(number)}: клієнт <b>{verdict}</b> "
                                    f"кошторис на {escape(sum_text)}")
        with self._lock:
            self._dirty.add(number)  # лічильник коментарів у картці
        self._wake.set()

    # --- розсилка ---

    def broadcast(self, messages: list[tuple[str, str]]) -> dict:
        """Надіслати підключеним клієнтам повідомлення: messages — [(ключ клієнта, текст)].
        Клієнтів, які заблокували бота, позначає й пропускає надалі."""
        sent, failed, skipped = 0, [], 0
        for key, text in messages:
            client = self.links["clients"].get(key)
            if not client or client.get("blocked"):
                skipped += 1
                continue
            try:
                self.api.call("sendMessage", chat_id=client["chat_id"], text=text[:4000])
                sent += 1
            except TelegramError as exc:
                failed.append(key)
                if any(x in str(exc).lower() for x in ("blocked", "deactivated", "chat not found")):
                    with self._lock:
                        client["blocked"] = True
            except httpx.HTTPError:
                failed.append(key)
            time.sleep(0.05)  # Telegram дозволяє до ~30 повідомлень на секунду
        result = {"at": now_str(), "sent": sent, "failed": len(failed), "skipped": skipped,
                  "text": messages[0][1][:200] if messages else ""}
        with self._lock:
            self.links.setdefault("broadcasts", []).append(result)
            self.links["broadcasts"] = self.links["broadcasts"][-30:]
            self._save_links()
        if self.chat_id and sent:
            try:
                self._send(f"📣 Розсилка клієнтам: надіслано {sent}" + (f", не доставлено {len(failed)}" if failed else ""))
            except (TelegramError, httpx.HTTPError):
                log.exception("Telegram: не вдалося написати в групу")
        return result

    # --- «Ваш автомобіль готовий» ---

    def _notify_ready(self, order: RepairOrder) -> None:
        if order.status != READY or order.number in self.links["ready_sent"]:
            return
        # Лише щойно встановлений статус: старим замовленням, що давно «готово», не пишемо.
        changed = next((x["at"] for x in reversed(order.status_log) if x.get("status") == READY), "")
        try:
            if datetime.now() - datetime.strptime(changed, "%d.%m.%Y %H:%M") > timedelta(hours=12):
                return
        except ValueError:
            return
        client = self.client_for(order)
        if client is None:
            return
        v = order.vehicle
        car = " ".join(x for x in (f"Tesla {v.model}" if v.model else "Tesla", f"({v.plate})" if v.plate else "") if x)
        lines = [f"✅ Ваш автомобіль {car} готовий!",
                 f"Замовлення-наряд № {order.number}." + self._to_pay(order).replace("\n", " ") + "."]
        place = ", ".join(x for x in (self.service_name, self.service_address) if x)
        if place:
            lines.append(f"Чекаємо на вас: {place}.")
        self.api.call("sendMessage", chat_id=client["chat_id"], text="\n".join(lines))
        self.links["ready_sent"][order.number] = now_str()
        if self.chat_id:
            self._note_in_group(order.number, f"📲 {escape(order.number)}: клієнту надіслано «автомобіль готовий»")

    # --- резервна копія ---

    def send_backup(self, chat_id: int | None = None) -> dict:
        chat_id = chat_id or self.backup_chat_id
        if not chat_id or self.make_backup is None:
            raise TelegramError("BACKUP_CHAT_ID не задано")
        data, info, filename = self.make_backup()
        caption = (f"🗄 Резервна копія {self.service_name}".rstrip() + f" за {datetime.now():%d.%m.%Y %H:%M}\n"
                   f"Замовлень: {info['orders']}, фото: {info['photos']}")
        if info.get("photos_skipped"):
            caption += f"\n⚠️ {info['photos_skipped']} фото не вмістились (ліміт Telegram 50 МБ) — " \
                       "завантажте повну копію в довіднику."
        self.api.send_document(chat_id, filename, data, caption, content_type="application/zip")
        return info

    def _backup_due(self) -> bool:
        now = datetime.now(KYIV) if KYIV else datetime.now()
        return now.hour >= BACKUP_HOUR and self.links["backup_date"] != now.strftime("%Y-%m-%d")

    def backup_tick(self) -> None:
        if not self.backup_chat_id or not self._backup_due():
            return
        self.send_backup()
        with self._lock:
            self.links["backup_date"] = (datetime.now(KYIV) if KYIV else datetime.now()).strftime("%Y-%m-%d")
            self._save_links()

    # --- підсумок дня власнику ---

    def daily_summary(self, day: datetime | None = None) -> str:
        day = day or datetime.now()  # той самий годинник, що й у журналі статусів (на сервері — київський час)
        today = day.strftime("%d.%m.%Y")
        orders = self.store.list()
        events = {s: [] for s in ("прийнято", "готово", "видано")}
        for o in orders:
            for e in o.status_log:
                if e.get("status") in events and e.get("at", "").startswith(today):
                    events[e["status"]].append(o)
        revenue = sum((o.totals()["total"] for o in {o.number: o for o in events["видано"]}.values()), 0)
        active = [o for o in orders if o.status in ACTIVE]
        now = day
        stale = []
        for o in orders:
            if o.status != "в роботі":
                continue
            since = next((e["at"] for e in reversed(o.status_log) if e.get("status") == "в роботі"), "")
            try:
                days = (now - datetime.strptime(since, "%d.%m.%Y %H:%M")).days
            except ValueError:
                continue
            if days >= STALE_DAYS:
                stale.append((days, o))
        pending = [n for n, a in self.links["approvals"].items() if a.get("status") == "pending"]
        load: dict[str, int] = {}
        for o in active:
            load[o.assignee or "без відповідального"] = load.get(o.assignee or "без відповідального", 0) + 1

        cash: dict[str, Decimal] = {}
        for o in orders:
            for p in o.payments:
                if p["at"].startswith(today):
                    cash[p["method"]] = cash.get(p["method"], Decimal("0")) + Decimal(p["amount"])
        debts = [o for o in orders if o.status == "видано" and o.balance() > 0]

        unique = lambda items: len({o.number for o in items})  # noqa: E731
        lines = [f"📊 <b>Підсумок дня {today}</b>" + (f" · {escape(self.service_name)}" if self.service_name else ""),
                 f"📥 Прийнято: {unique(events['прийнято'])}",
                 f"✅ Готово: {unique(events['готово'])}",
                 f"🏁 Видано: {unique(events['видано'])} · на суму <b>{money(revenue)} $</b>",
                 f"💵 Отримано: <b>{money(sum(cash.values(), Decimal('0')))} $</b>"
                 + (" (" + ", ".join(f"{escape(m)} {money(v)}" for m, v in cash.items()) + ")" if cash else ""),
                 f"🔧 Зараз у роботі: {len(active)}"]
        if debts:
            lines.append(f"⚠️ Борги клієнтів: {money(sum((o.balance() for o in debts), Decimal('0')))} $ "
                         f"({len(debts)} замовл.) — " + ", ".join(escape(o.number) for o in debts[:8]))
        if load:
            lines.append("👤 " + "; ".join(f"{escape(k)}: {v}" for k, v in sorted(load.items(), key=lambda x: -x[1])))
        if pending:
            lines.append(f"📝 Чекають погодження клієнтом: {len(pending)} — " + ", ".join(escape(n) for n in pending[:10]))
        if self.stock is not None and self.stock_parts is not None:
            low = [p for p in self.stock_parts().values() if self.stock.is_low(p.part_no)]
            if low:
                lines.append(f"📦 Треба замовити ({len(low)}): " + ", ".join(
                    f"{escape(p.name)} — {self.stock.qty(p.part_no):g}" for p in low[:10]))
        if stale:
            lines.append(f"⏰ Довго в роботі (понад {STALE_DAYS} дн.):")
            lines += [f"• {escape(o.number)} · {days} дн. · "
                      f"{escape(' '.join(x for x in (o.vehicle.model, o.vehicle.plate) if x) or '—')}"
                      f" · 👤 {escape(o.assignee or '—')}" for days, o in sorted(stale, key=lambda x: -x[0])[:15]]
        return "\n".join(lines)[:4000]

    def summary_tick(self) -> None:
        now = datetime.now()
        if not self.backup_chat_id or now.hour < SUMMARY_HOUR or self.links["summary_date"] == now.strftime("%Y-%m-%d"):
            return
        self.api.call("sendMessage", chat_id=self.backup_chat_id, text=self.daily_summary(now), parse_mode="HTML")
        with self._lock:
            self.links["summary_date"] = now.strftime("%Y-%m-%d")
            self._save_links()

    def _schedule_loop(self) -> None:
        while not self._stop.is_set():
            for name, tick in (("резервна копія", self.backup_tick), ("підсумок дня", self.summary_tick),
                               ("розклад записів", self.schedule_tick)):
                try:
                    tick()
                except Exception:
                    log.exception("Telegram: %s", name)
            self._stop.wait(600)

    def _note_in_group(self, number: str, text: str) -> None:
        link = self.links["orders"].get(number)
        if self.chat_id and link:
            try:
                self._remember(self._send(text, link["message_id"]), number)
            except (TelegramError, httpx.HTTPError):
                log.exception("Telegram: не вдалося написати в групу")

    def _private(self, msg: dict, text: str, command: str) -> None:
        chat_id = msg["chat"]["id"]
        if command == "/today":
            if self.backup_chat_id and chat_id == self.backup_chat_id:
                self.api.call("sendMessage", chat_id=chat_id, text=self.daily_summary(), parse_mode="HTML")
            else:
                self.api.call("sendMessage", chat_id=chat_id, text="Підсумок дня надсилається лише власнику.")
            return
        if command == "/backup":
            if not self.backup_chat_id or chat_id != self.backup_chat_id:
                self.api.call("sendMessage", chat_id=chat_id, text=(
                    f"Резервні копії надсилаються лише власнику. Ваш ID: {chat_id} — додайте його "
                    "як BACKUP_CHAT_ID у налаштуваннях сервера."))
                return
            self.api.call("sendMessage", chat_id=chat_id, text="Готую резервну копію…")
            self.send_backup(chat_id)
            return
        arg = text.split(maxsplit=1)[1].strip() if command == "/start" and " " in text else ""
        greeting = f"Це бот сервісу {self.service_name}." if self.service_name else "Це бот сервісу."
        if not arg.startswith("o_"):
            self.api.call("sendMessage", chat_id=chat_id,
                          text=f"{greeting} Сюди ми надсилатимемо ваші замовлення-наряди.")
            return
        number = self.links["client_tokens"].get(arg)
        try:
            order = self.store.load(number) if number else None
        except FileNotFoundError:
            order = None
        if order is None:
            self.api.call("sendMessage", chat_id=chat_id,
                          text="Посилання застаріло. Попросіть майстра надіслати замовлення-наряд ще раз.")
            return
        key = self.phone_key(order.customer.phone)
        if key:
            with self._lock:
                known = self.links["clients"].get(key, {})
                self.links["clients"][key] = {"chat_id": chat_id, "name": self.author(msg.get("from", {})),
                                              "at": known.get("at") if known.get("chat_id") == chat_id else now_str()}
                self._save_links()
        self.api.call("sendMessage", chat_id=chat_id,
                      text=f"Вітаємо! {greeting} Надсилаємо ваш замовлення-наряд № {order.number}.")
        self._send_order_document(chat_id, order)
        self._note_in_group(order.number, f"📲 {escape(order.number)}: клієнт підключився до бота й отримав "
                                          "замовлення-наряд")

    # --- фонові потоки ---

    def poll_once(self, timeout: int = 30) -> None:
        updates = self.api.call("getUpdates", offset=self.links["offset"] or None, timeout=timeout,
                                allowed_updates=["message", "callback_query"])
        for update in updates:
            try:
                self.handle_update(update)
            except (TelegramError, httpx.HTTPError):
                log.exception("Telegram: помилка обробки повідомлення")
            with self._lock:
                self.links["offset"] = update["update_id"] + 1
        if updates:
            with self._lock:
                self._save_links()

    def start(self) -> None:
        try:
            self.api.call("deleteWebhook")  # getUpdates не працює, якщо в бота лишився вебхук
        except (TelegramError, httpx.HTTPError):
            log.exception("Telegram: deleteWebhook")
        threading.Thread(target=self._sender_loop, name="telegram-send", daemon=True).start()
        threading.Thread(target=self._poll_loop, name="telegram-poll", daemon=True).start()
        threading.Thread(target=self._schedule_loop, name="telegram-schedule", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _sender_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=30)
            self._wake.clear()
            self._stop.wait(1.5)  # невелика пауза: кілька швидких змін — одне оновлення картки
            self.flush()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except (TelegramError, httpx.HTTPError):
                log.exception("Telegram: getUpdates")
                self._stop.wait(5)


def from_env(store: OrderStore, comments: CommentStore, data_dir: Path) -> TelegramBot | None:
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token:
        return None
    if not chat_id:
        log.warning("TELEGRAM_CHAT_ID не задано: бот лише відповідатиме на /chatid")
    bot = TelegramBot(TelegramAPI(token), int(chat_id or 0), store, comments, data_dir / "telegram.json")
    bot.backup_chat_id = int(os.environ.get("BACKUP_CHAT_ID") or 0)
    return bot
