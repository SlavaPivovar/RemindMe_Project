"""Telegram-бот для робочої групи: картки замовлень-нарядів і внутрішні коментарі.

- Коли замовлення-наряд виходить зі статусу «чернетка», бот публікує в групу картку й оновлює
  її при змінах; про зміну статусу пише окрему відповідь під карткою.
- Відповідь (Reply) на картку чи на повідомлення бота про замовлення стає внутрішнім коментарем;
  так само повідомлення, що починається з номера: «ЗН-2026-00001 замінили колодки».
- Коментарі з веб-інтерфейсу бот публікує відповіддю під карткою.
- Команди: /orders — замовлення в роботі, /order <номер> — картка з коментарями, /chatid — id чату.
Бот працює лише з однією групою (TELEGRAM_CHAT_ID); повідомлення з інших чатів ігноруються.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from html import escape
from pathlib import Path

import httpx

from .comments import CommentError, CommentStore
from .order import DRAFT, OrderStore, RepairOrder

log = logging.getLogger(__name__)

ACTIVE = ("прийнято", "в роботі", "готово")
STATUS_ICON = {"прийнято": "📥", "в роботі": "🔧", "готово": "✅", "видано": "🏁", "скасовано": "❌"}
NUMBER_IN_TEXT = re.compile(r"^\s*(ЗН-\d{4}-\d{5})[\s:,.\-–—]+(.+)$", re.S)
HELP = ("Я публікую сюди замовлення-наряди й збираю внутрішні коментарі.\n\n"
        "• Відповідайте (Reply) на картку замовлення — відповідь стане коментарем.\n"
        "• Або почніть повідомлення з номера: <code>ЗН-2026-00001 замінили колодки</code>.\n"
        "• /orders — замовлення в роботі\n• /order ЗН-2026-00001 — картка з коментарями")


class TelegramError(Exception):
    pass


class TelegramAPI:
    def __init__(self, token: str, http: httpx.Client | None = None):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.http = http or httpx.Client(timeout=45)

    def call(self, method: str, **params):
        response = self.http.post(self.base + method, json={k: v for k, v in params.items() if v is not None})
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

    # --- зв'язок повідомлень із замовленнями (файл на диску) ---

    def _load_links(self) -> dict:
        try:
            data = json.loads(self.links_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            data = {}
        return {"orders": data.get("orders", {}), "messages": data.get("messages", {}), "offset": data.get("offset", 0)}

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
        if not self.chat_id:
            return  # група ще не налаштована
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
        count = self.comments.count(order.number)
        if count:
            lines.append(f"💬 Коментарів: {count}")
        lines.append("<i>Відповідайте на це повідомлення, щоб залишити коментар.</i>")
        return "\n".join(lines)[:4000]

    def _send(self, text: str, reply_to: int | None = None) -> int:
        result = self.api.call("sendMessage", chat_id=self.chat_id, text=text, parse_mode="HTML",
                               reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True}
                               if reply_to else None)
        return result["message_id"]

    def _sync_order(self, number: str) -> None:
        try:
            order = self.store.load(number)
        except (FileNotFoundError, ValueError):
            return
        link = self.links["orders"].get(number)
        if link is None and order.status == DRAFT:
            return  # чернетки в групу не потрапляють
        text = self.card(order)
        if link is None:
            message_id = self._send(text)
            self.links["orders"][number] = {"message_id": message_id, "status": order.status, "text": text}
            self._remember(message_id, number)
            return
        if text != link["text"]:
            try:
                self.api.call("editMessageText", chat_id=self.chat_id, message_id=link["message_id"], text=text,
                              parse_mode="HTML")
            except TelegramError as exc:
                if "not modified" not in str(exc):
                    raise
            link["text"] = text
        if order.status != link["status"]:
            icon = STATUS_ICON.get(order.status, "📌")
            reply = self._send(f"{icon} {escape(number)}: статус «<b>{escape(order.status)}</b>»", link["message_id"])
            self._remember(reply, number)
            link["status"] = order.status

    def _run_task(self, kind: str, number: str, payload) -> None:
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
        msg = update.get("message")
        if not msg or msg.get("from", {}).get("is_bot"):
            return
        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or msg.get("caption") or "").strip()
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if command == "/chatid":  # для налаштування: дізнатися id групи
            self.api.call("sendMessage", chat_id=chat_id, text=f"ID цього чату: {chat_id}")
            return
        if chat_id != self.chat_id:
            return  # чужі чати ігноруємо
        if command in ("/start", "/help"):
            self.api.call("sendMessage", chat_id=chat_id, text=HELP, parse_mode="HTML")
        elif command == "/orders":
            self._list_active()
        elif command == "/order":
            self._show_order(text.split(maxsplit=1)[1] if " " in text else "")
        elif text and not command:
            self._comment_from(msg, text)

    def _comment_from(self, msg: dict, text: str) -> None:
        number = None
        reply = msg.get("reply_to_message")
        if reply:
            number = self.number_for_message(reply["message_id"])
        if number is None:
            match = NUMBER_IN_TEXT.match(text)
            if not match:
                return  # звичайна розмова в групі — не коментар
            number, text = match.group(1), match.group(2).strip()
        try:
            self.store.load(number)
        except FileNotFoundError:
            self.api.call("sendMessage", chat_id=self.chat_id, text=f"Замовлення-наряд {number} не знайдено",
                          reply_parameters={"message_id": msg["message_id"]})
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
        try:
            self.api.call("setMessageReaction", chat_id=self.chat_id, message_id=msg["message_id"],
                          reaction=[{"type": "emoji", "emoji": "👍"}])
        except TelegramError:
            pass  # реакції можуть бути вимкнені в групі — коментар однаково збережено
        self._wake.set()

    def _list_active(self) -> None:
        active = [o for o in self.store.list() if o.status in ACTIVE]
        if not active:
            text = "Зараз немає замовлень-нарядів у роботі."
        else:
            rows = [f"{STATUS_ICON[o.status]} <b>{escape(o.number)}</b> · {escape(o.status)} · "
                    f"{escape(' '.join(x for x in (o.vehicle.model, o.vehicle.plate) if x) or '—')} · "
                    f"{escape(o.customer.name or '—')}" for o in active]
            text = "Замовлення-наряди в роботі:\n" + "\n".join(rows)
        self.api.call("sendMessage", chat_id=self.chat_id, text=text[:4000], parse_mode="HTML")

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

    # --- фонові потоки ---

    def poll_once(self, timeout: int = 30) -> None:
        updates = self.api.call("getUpdates", offset=self.links["offset"] or None, timeout=timeout,
                                allowed_updates=["message"])
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
    return TelegramBot(TelegramAPI(token), int(chat_id or 0), store, comments, data_dir / "telegram.json")
