"""Веб-інтерфейс помічника: python -m tesla_ro.web"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import hmac
import html
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import anthropic
import httpx
import qrcode
import qrcode.image.svg
from anthropic.lib.tools import ToolError
from fastapi.concurrency import run_in_threadpool
from fastapi import Cookie, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from .assistant import MODEL, RepairOrderAssistant
from .backup import TELEGRAM_LIMIT, backup_filename, make_backup
from .catalog import DEFAULT_CATALOG, MODEL_CODES, Catalog, CatalogError
from .clients import build_clients, months_since, personalize
from .comments import CommentError, CommentStore
from .excel import ExcelError, export_catalog, export_clients, import_catalog
from .order import STATUSES, OrderStore, StatusError
from .photos import KINDS as PHOTO_KINDS
from .photos import PhotoError as GalleryError
from .photos import PhotoStore
from .render import PdfUnavailable, pdf_filename, render_html, render_pdf
from .tools import OrderSession
from .usage import UsageLedger
from .usage import add as add_usage
from .usage import empty as empty_usage
from .telegram import ClientNotConnected, TelegramError
from .telegram import from_env as telegram_from_env
from .vision import PhotoError, parse_data_url, recognize

STATIC = Path(__file__).parent / "static"
COOKIE = "tesla_ro_sid"
AUTH_COOKIE = "tesla_ro_auth"
AUTH_TTL = 30 * 24 * 3600  # вхід діє 30 днів
IDLE_TTL = 12 * 3600  # неактивні діалоги видаляються з пам'яті через 12 годин
FAIL_WINDOW = 15 * 60
MAX_FAILS = 10  # хибних паролів за 15 хвилин з однієї адреси
MAX_FAILS_TOTAL = 100  # і з усіх адрес разом (адресу за проксі можна підробити)


@dataclass
class Conversation:
    session: OrderSession
    assistant: RepairOrderAssistant
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_used: float = field(default_factory=time.monotonic)
    pending_usage: dict = field(default_factory=empty_usage)  # витрати до появи замовлення-наряду
    manual_changes: list[str] = field(default_factory=list)  # правки з панелі, про які ще не знає помічник


class ChatIn(BaseModel):
    text: str


class PhotoIn(BaseModel):
    image: str  # data:image/jpeg;base64,...


class StatusIn(BaseModel):
    status: str
    by: str = ""  # хто змінив (ім'я, яке співробітник указав на сайті)


class AssigneeIn(BaseModel):
    name: str = ""


class LineIn(BaseModel):
    """Новий рядок: з довідника (code / part_no) або власний (name + price)."""
    code: str = ""
    part_no: str = ""
    name: str = ""
    price: float | None = None
    qty: float = 1
    unit: str = "шт"
    include_parts: bool = True


class CustomerIn(BaseModel):
    name: str = ""
    phone: str = ""
    email: str = ""


class VehicleIn(BaseModel):
    vin: str = ""
    odometer_km: int | None = None
    plate: str = ""
    color: str = ""
    battery_percent: int | None = None


class TextIn(BaseModel):
    text: str


class GalleryPhotoIn(BaseModel):
    image: str  # data:image/jpeg;base64,...
    kind: str = "до"
    author: str = ""


class GalleryPhotoEditIn(BaseModel):
    kind: str | None = None
    to_client: bool | None = None


class ClientOrderIn(BaseModel):
    key: str
    vin: str = ""


class BroadcastIn(BaseModel):
    keys: list[str]
    text: str


class CommentIn(BaseModel):
    text: str
    author: str = ""


class LineEditIn(BaseModel):
    qty: float | None = None
    price: float | None = None
    discount_percent: float | None = None


MANUAL_NOTE = "[Зміни, внесені майстром вручну в панелі замовлення-наряду: "


@contextlib.contextmanager
def api_errors():
    """Перетворити помилки Anthropic API на зрозумілі повідомлення для інтерфейсу."""
    try:
        yield
    except anthropic.AuthenticationError:
        raise HTTPException(503, "Невірний ключ API Anthropic (ANTHROPIC_API_KEY)") from None
    except anthropic.RateLimitError:
        raise HTTPException(429, "Перевищено ліміт запитів до API, спробуйте за хвилину") from None
    except anthropic.APIStatusError as exc:
        raise HTTPException(502, f"Помилка API ({exc.status_code}): {exc.message}") from None
    except anthropic.APIConnectionError:
        raise HTTPException(502, "Немає з'єднання з API Anthropic") from None
    except TypeError as exc:
        if "authentication" not in str(exc):
            raise
        raise HTTPException(503, "Не задано ключ API: встановіть змінну оточення ANTHROPIC_API_KEY") from None


def create_app(
    catalog_path: str | Path | None = None,
    orders_dir: str | Path = "orders",
    client: anthropic.Anthropic | None = None,
    model: str = MODEL,
    effort: str = "medium",
    password: str | None = None,
    secure_cookies: bool = False,
    secret: str | None = None,
    usage_path: str | Path | None = None,
    telegram=None,
) -> FastAPI:
    """password — пароль для входу; без нього вхід не потрібен (лише для локального запуску).
    catalog_path — файл довідника, який редагується з інтерфейсу; за його відсутності створюється
    копія стандартного. Без catalog_path зміни довідника живуть лише в пам'яті."""
    if catalog_path:
        catalog = Catalog.load_editable(catalog_path)
    else:
        catalog = Catalog(json.loads(DEFAULT_CATALOG.read_text(encoding="utf-8")))
    store = OrderStore(orders_dir)
    ledger = UsageLedger(usage_path)
    comments = CommentStore(orders_dir)
    photos = PhotoStore(orders_dir)
    # telegram — фабрика (store, comments) -> TelegramBot | None; бот бачить кожне збереження замовлення.
    bot = telegram(store, comments) if telegram else None
    if bot:
        store.listeners.append(bot.order_saved)
        bot.render_pdf = lambda order: render_pdf(order, catalog.service)
        bot.service_name = catalog.service.get("name", "")
        bot.service_address = catalog.service.get("address", "")
        bot.photos = photos
        bot.make_backup = lambda: backup(TELEGRAM_LIMIT)
    client = client or anthropic.Anthropic()
    conversations: dict[str, Conversation] = {}
    conversations_lock = threading.Lock()
    service_name = catalog.service.get("name") or "Замовлення-наряди Tesla"
    app = FastAPI(title=service_name, docs_url=None, redoc_url=None, openapi_url=None)
    # Ключ підпису входу: зі сталого секрету (вхід переживає перезапуск) або випадковий.
    # Пароль теж входить у ключ, тож після зміни пароля всі входи стають недійсними.
    auth_key = hashlib.sha256(f"{secret or secrets.token_hex(32)}:{password}".encode()).digest()
    failures: dict[str, list[float]] = {}
    failures_lock = threading.Lock()

    def sign(expires: int) -> str:
        mac = hmac.new(auth_key, str(expires).encode(), hashlib.sha256).hexdigest()
        return f"{expires}.{mac}"

    def is_authorized(token: str | None) -> bool:
        if password is None:
            return True
        if not token or "." not in token:
            return False
        expires = token.split(".", 1)[0]
        return expires.isdigit() and int(expires) > time.time() and hmac.compare_digest(token, sign(int(expires)))

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        if request.url.path in ("/login", "/logout") or is_authorized(request.cookies.get(AUTH_COOKIE)):
            return await call_next(request)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "Потрібно увійти"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(error: str = ""):
        page = (STATIC / "login.html").read_text(encoding="utf-8")
        page = page.replace("<!--NAME-->", html.escape(catalog.service.get("name") or service_name))
        return page.replace("<!--ERROR-->", '<p class="err">Невірний пароль</p>' if error else "")

    @app.post("/login")
    def login(request: Request, password_input: str = Form("", alias="password")):
        ip = request.client.host if request.client else "?"
        now = time.time()
        with failures_lock:
            for key in list(failures):
                failures[key] = [t for t in failures[key] if now - t < FAIL_WINDOW]
                if not failures[key]:
                    del failures[key]
            total = sum(len(v) for v in failures.values())
            if len(failures.get(ip, [])) >= MAX_FAILS or total >= MAX_FAILS_TOTAL:
                return HTMLResponse("Забагато спроб входу. Спробуйте за 15 хвилин.", status_code=429)
        if password is None or hmac.compare_digest(password_input.encode(), password.encode()):
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(AUTH_COOKIE, sign(int(now) + AUTH_TTL), max_age=AUTH_TTL,
                            httponly=True, samesite="lax", secure=secure_cookies)
            return resp
        with failures_lock:
            failures.setdefault(ip, []).append(now)
        time.sleep(1)
        return RedirectResponse("/login?error=1", status_code=303)

    @app.get("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(AUTH_COOKIE)
        return resp

    def charge(conv: Conversation, kind: str, cost: dict | None) -> None:
        """Записати витрати в журнал і додати їх до поточного замовлення-наряду (якщо воно вже є)."""
        if not cost or not cost.get("requests"):
            return
        order = conv.session.order
        ledger.record(kind, cost, model, order.number if order else None)
        if order is None:
            add_usage(conv.pending_usage, cost)
            return
        order.ai_usage = add_usage(add_usage(dict(order.ai_usage), conv.pending_usage), cost)
        conv.pending_usage = empty_usage()
        conv.session._autosave()

    def new_conversation() -> Conversation:
        session = OrderSession(catalog, store)
        session.comments = comments
        session.extras = extras
        return Conversation(session, RepairOrderAssistant(session, client=client, model=model, effort=effort))

    def conversation(sid: str | None, response: Response) -> Conversation:
        now = time.monotonic()
        with conversations_lock:
            if sid and sid in conversations:
                conversations[sid].last_used = now
                return conversations[sid]
            for old in [k for k, c in conversations.items() if now - c.last_used > IDLE_TTL]:
                del conversations[old]
            sid = secrets.token_urlsafe(16)
            conversations[sid] = new_conversation()
            response.set_cookie(COOKIE, sid, httponly=True, samesite="lax", secure=secure_cookies)
            return conversations[sid]

    @app.get("/", response_class=HTMLResponse)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    def state(response: Response, tesla_ro_sid: str | None = Cookie(None)):
        conv = conversation(tesla_ro_sid, response)
        history = []
        for m in conv.assistant.messages:
            if isinstance(m["content"], str):
                text = m["content"]
                if text.startswith(MANUAL_NOTE):
                    text = text.split("]\n", 1)[-1]
                history.append({"role": "user", "text": text})
            elif m["role"] == "assistant":
                text = "\n".join(b.text for b in m["content"] if getattr(b, "type", None) == "text").strip()
                if text:
                    history.append({"role": "assistant", "text": text})
        return {"order": conv.session.snapshot(), "history": history, "service": catalog.service,
                "statuses": STATUSES, "telegram": bot is not None, "photo_kinds": PHOTO_KINDS,
                "backup_chat": bool(bot and bot.backup_chat_id),
                "assignees": sorted({o.assignee for o in store.list() if o.assignee}),
                "auth": password is not None}

    @app.post("/api/chat")
    def chat(body: ChatIn, response: Response, tesla_ro_sid: str | None = Cookie(None)):
        text = body.text.strip()
        if not text:
            raise HTTPException(400, "Порожнє повідомлення")
        conv = conversation(tesla_ro_sid, response)
        if not conv.lock.acquire(blocking=False):
            raise HTTPException(409, "Помічник ще відповідає на попереднє повідомлення")
        if conv.manual_changes:
            # Помічник має знати про ручні правки, інакше його підсумки розійдуться із замовленням.
            text = MANUAL_NOTE + "; ".join(conv.manual_changes) + "]\n" + text
        try:
            with api_errors():
                reply = conv.assistant.send(text)
            conv.manual_changes.clear()
        finally:
            charge(conv, "chat", conv.assistant.last_turn_usage)
            conv.lock.release()
        return {"reply": reply, "order": conv.session.snapshot()}

    @app.post("/api/photo")
    def photo(body: PhotoIn, response: Response, tesla_ro_sid: str | None = Cookie(None)):
        conv = conversation(tesla_ro_sid, response)
        try:
            media_type, data = parse_data_url(body.image)
            with api_errors():
                result = recognize(client, model, media_type, data)
        except PhotoError as exc:
            with conv.lock:
                charge(conv, "photo", exc.usage)
            raise HTTPException(400, str(exc)) from None
        with conv.lock:
            charge(conv, "photo", result["usage"])
        return result

    @app.get("/api/usage")
    def usage_summary():
        summary = ledger.summary()
        # Середнє — за самими замовленнями-нарядами: туди входять і витрати, зроблені до їх створення (фото).
        costs = [o.ai_usage.get("cost_usd", 0) for o in store.list() if o.ai_usage.get("requests")]
        summary["orders"] = len(costs)
        summary["avg_per_order"] = round(sum(costs) / len(costs), 4) if costs else 0
        return summary

    def edit_order(conv: Conversation, action, note: str):
        """Виконати ручну правку поточного замовлення (з автозбереженням) і запам'ятати її для помічника."""
        if conv.session.order is None:
            raise HTTPException(404, "Замовлення-наряд ще не створено")
        if not conv.lock.acquire(blocking=False):
            raise HTTPException(409, "Помічник ще відповідає — спробуйте за мить")
        try:
            try:
                extra = action()
            except ToolError as exc:
                raise HTTPException(400, str(exc)) from None
            conv.manual_changes.append(note)
        finally:
            conv.lock.release()
        return {"order": conv.session.snapshot(), **(extra if isinstance(extra, dict) else {})}

    def line_name(conv: Conversation, kind: str, number: int) -> str:
        lines = conv.session.order.labor if kind == "labor" else conv.session.order.parts
        return lines[number - 1].name if 1 <= number <= len(lines) else f"рядок {number}"

    @app.post("/api/order/labor")
    def add_labor(body: LineIn, response: Response, tesla_ro_sid: str | None = Cookie(None)):
        conv = conversation(tesla_ro_sid, response)
        s = conv.session
        if body.code:
            op = catalog.operations.get(body.code)
            return edit_order(conv, lambda: s.add_operation(body.code, body.include_parts, body.qty, body.price),
                              f"додав роботу «{op.name if op else body.code}» × {body.qty:g}")
        name = body.name.strip()
        if not name or body.price is None:
            raise HTTPException(400, "Вкажіть назву та ціну роботи")
        return edit_order(conv, lambda: s.add_custom_labor(name, body.price, body.qty),
                          f"додав роботу «{name}» за {body.price:g} × {body.qty:g}")

    @app.post("/api/order/parts")
    def add_part(body: LineIn, response: Response, tesla_ro_sid: str | None = Cookie(None)):
        conv = conversation(tesla_ro_sid, response)
        s = conv.session
        if body.part_no and body.price is None and not body.name:
            part = catalog.parts.get(body.part_no)
            return edit_order(conv, lambda: s.add_part(body.part_no, body.qty),
                              f"додав запчастину «{part.name if part else body.part_no}» × {body.qty:g}")
        name = body.name.strip()
        if not name or body.price is None:
            raise HTTPException(400, "Вкажіть назву та ціну запчастини")
        return edit_order(conv, lambda: s.add_custom_part(name, body.qty, body.price, body.part_no.strip(),
                                                          body.unit.strip() or "шт"),
                          f"додав запчастину «{name}» за {body.price:g} × {body.qty:g}")

    @app.patch("/api/order/{kind}/{number}")
    def update_line(kind: str, number: int, body: LineEditIn, response: Response,
                    tesla_ro_sid: str | None = Cookie(None)):
        if kind not in ("labor", "parts"):
            raise HTTPException(404, "Невідомий тип рядка")
        conv = conversation(tesla_ro_sid, response)
        s = conv.session

        def action():
            if body.price is not None or body.qty is not None:
                s.set_line(kind, number, body.price, body.qty)
            if body.discount_percent is not None:
                s.set_discount(kind, number, body.discount_percent)

        name = line_name(conv, kind, number) if s.order else ""
        changes = ", ".join(f"{label} {value:g}" for label, value in
                            (("кількість", body.qty), ("ціна", body.price), ("знижка %", body.discount_percent))
                            if value is not None)
        return edit_order(conv, action, f"змінив «{name}»: {changes}")

    @app.delete("/api/order/{kind}/{number}")
    def delete_line(kind: str, number: int, response: Response, tesla_ro_sid: str | None = Cookie(None)):
        """Видалити рядок роботи чи запчастини, скаргу або рекомендацію."""
        conv = conversation(tesla_ro_sid, response)
        s, order = conv.session, conv.session.order
        if kind in ("labor", "parts"):
            name = line_name(conv, kind, number) if order else ""
            return edit_order(conv, lambda: s.remove_line(kind, number), f"видалив «{name}»")
        if kind in ("complaints", "recommendations"):
            items = getattr(order, kind) if order else []
            text = items[number - 1] if 1 <= number <= len(items) else f"№{number}"
            remove = s.remove_complaint if kind == "complaints" else s.remove_recommendation
            label = "скаргу" if kind == "complaints" else "рекомендацію"
            return edit_order(conv, lambda: remove(number), f"видалив {label} «{text}»")
        raise HTTPException(404, "Невідомий тип рядка")

    @app.post("/api/order/status")
    def order_status(body: StatusIn, response: Response, tesla_ro_sid: str | None = Cookie(None)):
        conv = conversation(tesla_ro_sid, response)
        if conv.session.order is None:
            raise HTTPException(404, "Замовлення-наряд ще не створено")
        if not conv.lock.acquire(blocking=False):
            raise HTTPException(409, "Помічник ще відповідає — спробуйте за мить")
        try:
            try:
                conv.session.order.set_status(body.status, by=body.by.strip()[:80])
            except StatusError as exc:
                raise HTTPException(400, str(exc)) from None
            try:
                store.save(conv.session.order)
            except OSError:
                raise HTTPException(500, "Не вдалося зберегти замовлення-наряд") from None
        finally:
            conv.lock.release()
        return {"order": conv.session.snapshot()}

    @app.put("/api/order/assignee")
    def set_assignee(body: AssigneeIn, response: Response, tesla_ro_sid: str | None = Cookie(None)):
        conv = conversation(tesla_ro_sid, response)
        name = " ".join(body.name.split())[:80]

        def action():
            order = conv.session.order
            if name != order.assignee:
                order.assignee, order.assignee_tg = name, None
                conv.session._autosave()
        return edit_order(conv, action, f"відповідальний: {name or 'не призначено'}")

    def sync_open_orders(order) -> None:
        """Зміну з Telegram (статус, відповідальний) переносимо у відкриті на сайті копії замовлення,
        інакше наступне автозбереження з браузера повернуло б старе."""
        with conversations_lock:
            sessions = [c.session for c in conversations.values()]
        for session in sessions:
            current = session.order
            if current is not None and current.number == order.number and current is not order:
                current.status, current.status_log = order.status, list(order.status_log)
                current.assignee, current.assignee_tg = order.assignee, order.assignee_tg

    if bot:
        bot.on_order_changed = sync_open_orders

    @app.get("/voice/{number}/{name}")
    def voice(number: str, name: str):
        try:
            path = comments.voice_path(number, name)
        except (FileNotFoundError, CommentError):
            raise HTTPException(404, "Запис не знайдено") from None
        media = {".mp3": "audio/mpeg", ".m4a": "audio/mp4"}.get(path.suffix, "audio/ogg")
        return FileResponse(path, media_type=media, headers={"Cache-Control": "private, max-age=86400"})

    def restart(conv: Conversation) -> None:
        """Почати з чистого аркуша: нова розмова з помічником без замовлення-наряду."""
        with conversations_lock:
            fresh = new_conversation()
            conv.session, conv.assistant = fresh.session, fresh.assistant
            conv.manual_changes.clear()

    @app.post("/api/reset")
    def reset(response: Response, tesla_ro_sid: str | None = Cookie(None)):
        restart(conversation(tesla_ro_sid, response))
        return {"order": None}

    @app.post("/api/order/new")
    def new_order_manual(response: Response, tesla_ro_sid: str | None = Cookie(None)):
        """Порожнє замовлення-наряд для ручного заповнення (без помічника)."""
        conv = conversation(tesla_ro_sid, response)
        if conv.lock.locked():
            raise HTTPException(409, "Помічник ще відповідає — спробуйте за мить")
        restart(conv)
        conv.session.new_order()
        conv.manual_changes.append("створив нове порожнє замовлення-наряд")
        return {"order": conv.session.snapshot()}

    @app.delete("/api/orders/{number}")
    def delete_order(number: str):
        try:
            store.delete(number)
        except FileNotFoundError:
            raise HTTPException(404, "Замовлення-наряд не знайдено") from None
        if bot:
            bot.order_deleted(number)
        # Хто тримав це замовлення відкритим — починає з чистого аркуша.
        with conversations_lock:
            affected = [c for c in conversations.values() if c.session.order and c.session.order.number == number]
        for c in affected:
            restart(c)
        return {"deleted": number}

    @app.put("/api/order/customer")
    def set_customer(body: CustomerIn, response: Response, tesla_ro_sid: str | None = Cookie(None)):
        conv = conversation(tesla_ro_sid, response)
        return edit_order(conv, lambda: conv.session.set_customer(body.name, body.phone, body.email),
                          f"замовник: {body.name}, {body.phone}")

    @app.put("/api/order/vehicle")
    def set_vehicle(body: VehicleIn, response: Response, tesla_ro_sid: str | None = Cookie(None)):
        conv = conversation(tesla_ro_sid, response)

        def action():
            result = json.loads(conv.session.set_vehicle(body.vin, body.odometer_km, body.plate, body.color,
                                                         body.battery_percent))
            return {"warnings": result["vin_warnings"]}

        return edit_order(conv, action, f"автомобіль: VIN {body.vin}, пробіг {body.odometer_km}")

    @app.post("/api/order/complaints")
    def add_complaint(body: TextIn, response: Response, tesla_ro_sid: str | None = Cookie(None)):
        conv = conversation(tesla_ro_sid, response)
        text = body.text.strip()
        if not text:
            raise HTTPException(400, "Порожня скарга")
        return edit_order(conv, lambda: conv.session.add_complaint(text), f"додав скаргу «{text}»")

    @app.post("/api/order/recommendations")
    def add_recommendation(body: TextIn, response: Response, tesla_ro_sid: str | None = Cookie(None)):
        conv = conversation(tesla_ro_sid, response)
        text = body.text.strip()
        if not text:
            raise HTTPException(400, "Порожня рекомендація")
        return edit_order(conv, lambda: conv.session.add_recommendation(text),
                          f"додав рекомендацію «{text}»")

    @app.put("/api/order/diagnosis")
    def set_diagnosis(body: TextIn, response: Response, tesla_ro_sid: str | None = Cookie(None)):
        conv = conversation(tesla_ro_sid, response)
        return edit_order(conv, lambda: conv.session.set_diagnosis(body.text),
                          f"діагностика: «{body.text.strip()}»")

    model_names = {"MODEL_S": "Model S", "MODEL_3": "Model 3", "MODEL_X": "Model X", "MODEL_Y": "Model Y",
                   "CYBERTRUCK": "Cybertruck"}

    @app.get("/api/catalog")
    def catalog_view():
        operations, parts = catalog.operations, catalog.parts  # знімок на випадок паралельного редагування
        ops = []
        for o in operations.values():
            op_parts = [parts[p] for p in o.parts if p in parts]
            ops.append({
                "code": o.code, "name": o.name, "category": o.category, "price": o.price,
                "model_codes": o.models,
                "models": "усі" if "*" in o.models else ", ".join(model_names.get(m, m) for m in o.models),
                "parts": [{"part_no": p.part_no, "name": p.name, "price": p.price} for p in op_parts],
                "parts_price": round(sum(p.price for p in op_parts), 2),
                "interval": o.interval,
            })
        used = {}
        for o in operations.values():
            for p in o.parts:
                used[p] = used.get(p, 0) + 1
        return {
            "categories": catalog.categories(), "operations": ops, "currency": catalog.currency,
            "parts": [{"part_no": p.part_no, "name": p.name, "price": p.price, "unit": p.unit,
                       "used": used.get(p.part_no, 0)} for p in parts.values()],
            "models": [{"code": m, "name": model_names[m]} for m in MODEL_CODES],
            "settings": {**catalog.service, "vat_percent": catalog.vat_percent, "prices_include_vat": catalog.prices_include_vat},
        }

    def edit(action, *args):
        try:
            action(*args)
        except CatalogError as exc:
            raise HTTPException(400, str(exc)) from None
        except KeyError:
            raise HTTPException(404, "Запис не знайдено (можливо, його вже видалили)") from None
        except OSError:
            raise HTTPException(500, "Не вдалося зберегти довідник на диск") from None
        return catalog_view()

    @app.post("/api/catalog/operations")
    def create_operation(body: dict):
        return edit(catalog.save_operation, body, True)

    @app.put("/api/catalog/operations/{code}")
    def update_operation(code: str, body: dict):
        return edit(catalog.save_operation, {**body, "code": code}, False)

    @app.delete("/api/catalog/operations/{code}")
    def delete_operation(code: str):
        return edit(catalog.delete_operation, code)

    @app.post("/api/catalog/parts")
    def create_part(body: dict):
        return edit(catalog.save_part, body, True)

    @app.put("/api/catalog/parts/{part_no}")
    def update_part(part_no: str, body: dict):
        return edit(catalog.save_part, {**body, "part_no": part_no}, False)

    @app.delete("/api/catalog/parts/{part_no}")
    def delete_part(part_no: str):
        return edit(catalog.delete_part, part_no)

    @app.put("/api/catalog/settings")
    def update_settings(body: dict):
        result = edit(catalog.update_settings, body)
        if bot:
            bot.service_name = catalog.service.get("name", "")
            bot.service_address = catalog.service.get("address", "")
        return result

    # --- прайс в Excel і резервна копія ---

    @app.get("/api/catalog/export.xlsx")
    def catalog_export():
        name = quote(f"prais-{time.strftime('%Y-%m-%d')}.xlsx")
        return Response(export_catalog(catalog),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})

    @app.post("/api/catalog/import")
    async def catalog_import(request: Request, apply: bool = False):
        data = await request.body()
        if not data:
            raise HTTPException(400, "Файл порожній")
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(400, "Файл завеликий (до 10 МБ)")
        try:
            preview = await run_in_threadpool(import_catalog, catalog, data, apply)
        except ExcelError as exc:
            raise HTTPException(400, str(exc)) from None
        except OSError:
            raise HTTPException(500, "Не вдалося зберегти довідник на диск") from None
        return {**preview, "catalog": catalog_view() if apply else None}

    def backup(max_bytes: int | None = None):
        extra = [p for p in (catalog.path, ledger.path, bot.links_path if bot else None) if p]
        data, info = make_backup(Path(orders_dir), extra, max_bytes)
        return data, info, backup_filename(catalog.service.get("name", ""))

    @app.get("/api/backup.zip")
    def backup_download():
        data, _, filename = backup()
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})

    @app.post("/api/backup/telegram")
    def backup_telegram():
        if not bot or not bot.backup_chat_id:
            raise HTTPException(503, "Не задано BACKUP_CHAT_ID — куди надсилати копію")
        try:
            return bot.send_backup()
        except (TelegramError, httpx.HTTPError) as exc:
            raise HTTPException(502, f"Telegram: {exc}") from None

    def matches(o, q: str) -> bool:
        """Пошук за номером, VIN, держномером, ПІБ або телефоном (телефон — лише за цифрами)."""
        text = " ".join((o.number, o.vehicle.vin, o.vehicle.plate, o.customer.name, o.vehicle.model)).lower()
        phone = "".join(ch for ch in o.customer.phone if ch.isdigit())
        digits = "".join(ch for ch in q if ch.isdigit())
        if len(digits) >= 3 and all(ch.isdigit() or ch in " +-()" for ch in q):
            return digits in phone or q.lower() in text  # схоже на телефон: «067 123 45», «+380…»
        return all(w in text for w in q.lower().split())

    @app.get("/api/orders")
    def orders(q: str = "", status: str = ""):
        q = q.strip()
        return [
            {
                "number": o.number,
                "created_at": o.created_at,
                "status": o.status,
                "customer": o.customer.name,
                "phone": o.customer.phone,
                "vin": o.vehicle.vin,
                "vehicle": " ".join(x for x in (o.vehicle.model, str(o.vehicle.year or ""), o.vehicle.plate) if x),
                "total": str(o.totals()["total"]),
                "currency": o.currency,
                "ai_cost": o.ai_usage.get("cost_usd", 0),
                "comments": comments.count(o.number),
            }
            for o in store.list()
            if (not q or matches(o, q)) and (not status or o.status == status)
        ]

    @app.post("/api/orders/{number}/open")
    def open_order(number: str, response: Response, tesla_ro_sid: str | None = Cookie(None)):
        conv = conversation(tesla_ro_sid, response)
        try:
            order = store.load(number)
        except FileNotFoundError:
            raise HTTPException(404, "Замовлення-наряд не знайдено") from None
        # Починаємо новий діалог, щоб історія не стосувалася іншого замовлення.
        with conversations_lock:
            fresh = new_conversation()
            conv.session, conv.assistant = fresh.session, fresh.assistant
            conv.manual_changes.clear()
        conv.session.order = order
        return {"order": conv.session.snapshot()}

    def pdf_response(order) -> Response:
        try:
            pdf = render_pdf(order, catalog.service)
        except PdfUnavailable:
            raise HTTPException(503, "PDF недоступний на сервері — скористайтеся «Друк» → «Зберегти як PDF»") from None
        name = quote(pdf_filename(order))
        return Response(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f"inline; filename*=UTF-8''{name}"})

    @app.get("/pdf/current")
    def pdf_current(response: Response, tesla_ro_sid: str | None = Cookie(None)):
        conv = conversation(tesla_ro_sid, response)
        if conv.session.order is None:
            raise HTTPException(404, "Замовлення-наряд ще не створено")
        return pdf_response(conv.session.order)

    @app.get("/pdf/{number}")
    def pdf_saved(number: str):
        try:
            return pdf_response(store.load(number))
        except FileNotFoundError:
            raise HTTPException(404, "Замовлення-наряд не знайдено") from None

    def saved_order_for_client(number: str):
        if not bot:
            raise HTTPException(503, "Telegram-бот не налаштований")
        try:
            return store.load(number)
        except FileNotFoundError:
            raise HTTPException(404, "Замовлення-наряд ще не збережено — спершу заповніть його") from None

    @app.post("/api/orders/{number}/client-link")
    def client_link(number: str):
        """Посилання й QR-код: клієнт відкриває бота, натискає «Старт» і отримує PDF замовлення-наряду."""
        order = saved_order_for_client(number)
        try:
            link = bot.client_link(number)
        except (TelegramError, httpx.HTTPError):
            raise HTTPException(502, "Не вдалося зв'язатися з Telegram") from None
        qr = qrcode.make(link, image_factory=qrcode.image.svg.SvgPathImage, box_size=8).to_string().decode()
        client = bot.client_for(order)
        return {"link": link, "qr_svg": qr, "client": client["name"] if client else None,
                "has_phone": bot.phone_key(order.customer.phone) is not None}

    @app.post("/api/orders/{number}/send-approval")
    def send_approval(number: str):
        order = saved_order_for_client(number)
        if not order.labor and not order.parts:
            raise HTTPException(400, "У кошторисі ще немає робіт і запчастин")
        try:
            approval = bot.send_for_approval(order)
        except ClientNotConnected:
            raise HTTPException(409, "Клієнт ще не підключив бота — покажіть йому QR-код або надішліть посилання") from None
        except PdfUnavailable:
            raise HTTPException(503, "PDF недоступний на сервері") from None
        except (TelegramError, httpx.HTTPError) as exc:
            raise HTTPException(502, f"Telegram: {exc}") from None
        return {"approval": approval}

    @app.post("/api/orders/{number}/send-telegram")
    def send_telegram(number: str):
        order = saved_order_for_client(number)
        try:
            client = bot.send_to_client(order)
        except ClientNotConnected:
            raise HTTPException(409, "Клієнт ще не підключив бота — покажіть йому QR-код або надішліть посилання") from None
        except PdfUnavailable:
            raise HTTPException(503, "PDF недоступний на сервері") from None
        except (TelegramError, httpx.HTTPError) as exc:
            raise HTTPException(502, f"Telegram: {exc}") from None
        return {"sent": True, "client": client["name"], "photos": client.get("photos", 0)}

    @app.get("/print/current", response_class=HTMLResponse)
    def print_current(response: Response, tesla_ro_sid: str | None = Cookie(None)):
        conv = conversation(tesla_ro_sid, response)
        if conv.session.order is None:
            raise HTTPException(404, "Замовлення-наряд ще не створено")
        return render_html(conv.session.order, catalog.service)

    @app.get("/print/{number}", response_class=HTMLResponse)
    def print_saved(number: str):
        try:
            return render_html(store.load(number), catalog.service)
        except FileNotFoundError:
            raise HTTPException(404, "Замовлення-наряд не знайдено") from None

    @app.exception_handler(HTTPException)
    def http_error(_, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    @app.get("/api/orders/{number}/comments")
    def list_comments(number: str):
        return comments.list(number)

    @app.post("/api/orders/{number}/comments")
    def add_comment(number: str, body: CommentIn):
        try:
            store.load(number)
        except FileNotFoundError:
            raise HTTPException(404, "Замовлення-наряд ще не збережено — спершу заповніть його") from None
        try:
            comment = comments.add(number, body.author or "Співробітник", body.text, source="web")
        except CommentError as exc:
            raise HTTPException(400, str(exc)) from None
        if bot:
            bot.comment_added(number, comment)
        return comments.list(number)

    # --- база клієнтів і розсилка ---

    def all_clients() -> list[dict]:
        return build_clients(store.list(), bot.links["clients"] if bot else None)

    @app.get("/api/clients")
    def clients_view():
        clients = all_clients()
        for c in clients:
            c["months"] = months_since(c["last_visit"])
        return {"clients": clients, "telegram": bot is not None,
                "broadcasts": list(reversed(bot.links.get("broadcasts", []))) if bot else []}

    @app.get("/api/clients/export.xlsx")
    def clients_export():
        return Response(export_clients(all_clients()),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''klienty-{time.strftime('%Y-%m-%d')}.xlsx"})

    @app.post("/api/clients/new-order")
    def client_new_order(body: ClientOrderIn, response: Response, tesla_ro_sid: str | None = Cookie(None)):
        """Нове замовлення-наряд для відомого клієнта: ПІБ, телефон і (якщо вибрано) автомобіль підставляються."""
        client = next((c for c in all_clients() if c["key"] == body.key), None)
        if client is None:
            raise HTTPException(404, "Клієнта не знайдено")
        conv = conversation(tesla_ro_sid, response)
        if conv.lock.locked():
            raise HTTPException(409, "Помічник ще відповідає — спробуйте за мить")
        restart(conv)
        conv.session.new_order()
        conv.session.set_customer(client["name"], client["phone"], client["email"])
        note = f"створив нове замовлення-наряд для клієнта {client['name']}"
        car = next((c for c in client["cars"] if body.vin and c.get("vin") == body.vin), None)
        if car:
            try:
                conv.session.set_vehicle(car["vin"], plate=car.get("plate", ""))
                note += f", автомобіль VIN {car['vin']} (пробіг треба уточнити)"
            except ToolError:
                pass
        conv.manual_changes.append(note)
        return {"order": conv.session.snapshot()}

    @app.post("/api/broadcast")
    def broadcast(body: BroadcastIn):
        if not bot:
            raise HTTPException(503, "Telegram-бот не налаштований")
        text = body.text.strip()
        if not text:
            raise HTTPException(400, "Порожнє повідомлення")
        if len(text) > 3000:
            raise HTTPException(400, "Повідомлення задовге (до 3000 символів)")
        keys = list(dict.fromkeys(body.keys))
        if not keys:
            raise HTTPException(400, "Не вибрано жодного клієнта")
        if len(keys) > 1000:
            raise HTTPException(400, "Забагато отримувачів за раз (до 1000)")
        clients = {c["key"]: c for c in all_clients()}
        messages = [(k, personalize(text, clients[k], catalog.service)) for k in keys if k in clients]
        return bot.broadcast(messages)

    # --- фото до замовлення ---

    def extras(number: str) -> dict:
        return {"photos": photos.list(number), "approval": bot.approval(number) if bot else None}

    @app.get("/api/orders/{number}/extras")
    def order_extras(number: str):
        result = {**extras(number), "comments": comments.list(number)}
        with contextlib.suppress(FileNotFoundError, ValueError):
            saved = store.load(number)  # статус і відповідального могли змінити кнопками в Telegram
            result.update(status=saved.status, assignee=saved.assignee)
        return result

    def saved_order(number: str):
        try:
            return store.load(number)
        except FileNotFoundError:
            raise HTTPException(404, "Замовлення-наряд ще не збережено — спершу заповніть його") from None

    @app.post("/api/orders/{number}/photos")
    def add_gallery_photo(number: str, body: GalleryPhotoIn):
        saved_order(number)
        try:
            media_type, data = parse_data_url(body.image)
            photos.add(number, base64.b64decode(data), media_type, body.kind, body.author or "Співробітник")
        except (PhotoError, GalleryError) as exc:
            raise HTTPException(400, str(exc)) from None
        return photos.list(number)

    @app.patch("/api/orders/{number}/photos/{photo_id}")
    def edit_gallery_photo(number: str, photo_id: str, body: GalleryPhotoEditIn):
        try:
            photos.update(number, photo_id, body.kind, body.to_client)
        except GalleryError as exc:
            raise HTTPException(400, str(exc)) from None
        except (KeyError, FileNotFoundError):
            raise HTTPException(404, "Фото не знайдено") from None
        return photos.list(number)

    @app.delete("/api/orders/{number}/photos/{photo_id}")
    def delete_gallery_photo(number: str, photo_id: str):
        try:
            photos.delete(number, photo_id)
        except (KeyError, FileNotFoundError):
            raise HTTPException(404, "Фото не знайдено") from None
        return photos.list(number)

    @app.get("/photos/{number}/{photo_id}")
    def gallery_photo(number: str, photo_id: str):
        try:
            path = photos.path(number, photo_id)
        except (KeyError, FileNotFoundError):
            raise HTTPException(404, "Фото не знайдено") from None
        return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})

    app.state.telegram = bot
    return app


def load_secret(data_dir: Path) -> str:
    """Сталий секрет для підпису входу: створюється один раз і лежить на диску поруч із даними."""
    path = data_dir / ".auth_secret"
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        value = secrets.token_hex(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        path.chmod(0o600)
        return value


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    # Час у замовленнях, коментарях і журналах — київський, навіть якщо сервер працює в UTC (Render).
    os.environ.setdefault("TZ", "Europe/Kyiv")
    if hasattr(time, "tzset"):
        time.tzset()
    env = os.environ.get
    parser = argparse.ArgumentParser(description="Веб-інтерфейс помічника із замовлень-нарядів Tesla")
    parser.add_argument("--host", default=env("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(env("PORT", "8000")))
    parser.add_argument("--orders-dir", default=env("ORDERS_DIR", "orders"))
    parser.add_argument("--catalog", default=env("CATALOG"),
                        help="файл довідника, який редагується з інтерфейсу (за замовчуванням catalog.json "
                             "поруч із каталогом замовлень)")
    parser.add_argument("--model", default=env("MODEL", MODEL))
    parser.add_argument("--effort", default=env("EFFORT", "medium"), choices=["low", "medium", "high", "xhigh", "max"])
    args = parser.parse_args(argv)

    password = env("APP_PASSWORD") or None
    local = args.host in ("127.0.0.1", "localhost", "::1")
    if not local and not password:
        print("Для доступу з мережі задайте пароль: змінна оточення APP_PASSWORD.")
        return 2
    if password and len(password) < 8:
        print("APP_PASSWORD закороткий: потрібно щонайменше 8 символів.")
        return 2
    secure = env("COOKIE_SECURE", "0" if local else "1") == "1"

    data_dir = Path(args.orders_dir).resolve().parent
    catalog_path = args.catalog or data_dir / "catalog.json"
    app = create_app(catalog_path, args.orders_dir, model=args.model, effort=args.effort,
                     password=password, secure_cookies=secure, secret=load_secret(data_dir),
                     usage_path=data_dir / "usage.jsonl",
                     telegram=lambda store, comments: telegram_from_env(store, comments, data_dir))
    if app.state.telegram:
        app.state.telegram.start()
        print("Telegram-бот увімкнено")
    print(f"Відкрийте в браузері: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="*")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
