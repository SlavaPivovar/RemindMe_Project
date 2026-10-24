"""Веб-інтерфейс помічника: python -m tesla_ro.web"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
from fastapi import Cookie, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from .assistant import MODEL, RepairOrderAssistant
from .catalog import DEFAULT_CATALOG, Catalog
from .order import OrderStore
from .render import render_html
from .tools import OrderSession

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


class ChatIn(BaseModel):
    text: str


def create_app(
    catalog_path: str | Path = DEFAULT_CATALOG,
    orders_dir: str | Path = "orders",
    client: anthropic.Anthropic | None = None,
    model: str = MODEL,
    effort: str = "medium",
    password: str | None = None,
    secure_cookies: bool = False,
) -> FastAPI:
    """password — пароль для входу; без нього вхід не потрібен (лише для локального запуску)."""
    catalog = Catalog.load(catalog_path)
    store = OrderStore(orders_dir)
    client = client or anthropic.Anthropic()
    conversations: dict[str, Conversation] = {}
    conversations_lock = threading.Lock()
    service_name = catalog.service.get("name") or "Замовлення-наряди Tesla"
    app = FastAPI(title=service_name, docs_url=None, redoc_url=None, openapi_url=None)
    auth_key = hashlib.sha256(f"{secrets.token_hex(32)}:{password}".encode()).digest()
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
        page = page.replace("<!--NAME-->", html.escape(service_name))
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

    def new_conversation() -> Conversation:
        session = OrderSession(catalog, store)
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
                history.append({"role": "user", "text": m["content"]})
            elif m["role"] == "assistant":
                text = "\n".join(b.text for b in m["content"] if getattr(b, "type", None) == "text").strip()
                if text:
                    history.append({"role": "assistant", "text": text})
        return {"order": conv.session.snapshot(), "history": history, "service": catalog.service,
                "auth": password is not None}

    @app.post("/api/chat")
    def chat(body: ChatIn, response: Response, tesla_ro_sid: str | None = Cookie(None)):
        text = body.text.strip()
        if not text:
            raise HTTPException(400, "Порожнє повідомлення")
        conv = conversation(tesla_ro_sid, response)
        if not conv.lock.acquire(blocking=False):
            raise HTTPException(409, "Помічник ще відповідає на попереднє повідомлення")
        try:
            reply = conv.assistant.send(text)
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
        finally:
            conv.lock.release()
        return {"reply": reply, "order": conv.session.snapshot()}

    @app.post("/api/reset")
    def reset(response: Response, tesla_ro_sid: str | None = Cookie(None)):
        conv = conversation(tesla_ro_sid, response)
        with conversations_lock:
            fresh = new_conversation()
            conv.session, conv.assistant = fresh.session, fresh.assistant
        return {"order": None}

    @app.get("/api/orders")
    def orders():
        return [
            {
                "number": o.number,
                "created_at": o.created_at,
                "status": o.status,
                "customer": o.customer.name,
                "vehicle": " ".join(x for x in (o.vehicle.model, str(o.vehicle.year or ""), o.vehicle.plate) if x),
                "total": str(o.totals()["total"]),
                "currency": o.currency,
            }
            for o in store.list()
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
        conv.session.order = order
        return {"order": conv.session.snapshot()}

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

    return app


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    env = os.environ.get
    parser = argparse.ArgumentParser(description="Веб-інтерфейс помічника із замовлень-нарядів Tesla")
    parser.add_argument("--host", default=env("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(env("PORT", "8000")))
    parser.add_argument("--catalog", default=env("CATALOG", str(DEFAULT_CATALOG)))
    parser.add_argument("--orders-dir", default=env("ORDERS_DIR", "orders"))
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

    app = create_app(args.catalog, args.orders_dir, model=args.model, effort=args.effort,
                     password=password, secure_cookies=secure)
    print(f"Відкрийте в браузері: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="*")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
