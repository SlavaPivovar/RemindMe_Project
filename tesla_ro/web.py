"""Веб-интерфейс помощника: python -m tesla_ro.web"""

from __future__ import annotations

import argparse
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
from fastapi import Cookie, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from .assistant import MODEL, RepairOrderAssistant
from .catalog import DEFAULT_CATALOG, Catalog
from .order import OrderStore
from .render import render_html
from .tools import OrderSession

STATIC = Path(__file__).parent / "static"
COOKIE = "tesla_ro_sid"


@dataclass
class Conversation:
    session: OrderSession
    assistant: RepairOrderAssistant
    lock: threading.Lock = field(default_factory=threading.Lock)


class ChatIn(BaseModel):
    text: str


def create_app(
    catalog_path: str | Path = DEFAULT_CATALOG,
    orders_dir: str | Path = "orders",
    client: anthropic.Anthropic | None = None,
    model: str = MODEL,
    effort: str = "medium",
) -> FastAPI:
    catalog = Catalog.load(catalog_path)
    store = OrderStore(orders_dir)
    client = client or anthropic.Anthropic()
    conversations: dict[str, Conversation] = {}
    conversations_lock = threading.Lock()
    app = FastAPI(title="Заказ-наряды Tesla")

    def new_conversation() -> Conversation:
        session = OrderSession(catalog, store)
        return Conversation(session, RepairOrderAssistant(session, client=client, model=model, effort=effort))

    def conversation(sid: str | None, response: Response) -> Conversation:
        with conversations_lock:
            if sid and sid in conversations:
                return conversations[sid]
            sid = secrets.token_urlsafe(16)
            conversations[sid] = new_conversation()
            response.set_cookie(COOKIE, sid, httponly=True, samesite="lax")
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
        return {"order": conv.session.snapshot(), "history": history, "service": catalog.service}

    @app.post("/api/chat")
    def chat(body: ChatIn, response: Response, tesla_ro_sid: str | None = Cookie(None)):
        text = body.text.strip()
        if not text:
            raise HTTPException(400, "Пустое сообщение")
        conv = conversation(tesla_ro_sid, response)
        if not conv.lock.acquire(blocking=False):
            raise HTTPException(409, "Помощник ещё отвечает на предыдущее сообщение")
        try:
            reply = conv.assistant.send(text)
        except anthropic.AuthenticationError:
            raise HTTPException(503, "Неверный ключ API Anthropic (ANTHROPIC_API_KEY)") from None
        except anthropic.RateLimitError:
            raise HTTPException(429, "Превышен лимит запросов к API, попробуйте через минуту") from None
        except anthropic.APIStatusError as exc:
            raise HTTPException(502, f"Ошибка API ({exc.status_code}): {exc.message}") from None
        except anthropic.APIConnectionError:
            raise HTTPException(502, "Нет соединения с API Anthropic") from None
        except TypeError as exc:
            if "authentication" not in str(exc):
                raise
            raise HTTPException(503, "Не задан ключ API: установите переменную окружения ANTHROPIC_API_KEY") from None
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
            raise HTTPException(404, "Заказ-наряд не найден") from None
        # Начинаем новый диалог, чтобы история не относилась к другому заказу.
        with conversations_lock:
            fresh = new_conversation()
            conv.session, conv.assistant = fresh.session, fresh.assistant
        conv.session.order = order
        return {"order": conv.session.snapshot()}

    @app.get("/print/current", response_class=HTMLResponse)
    def print_current(response: Response, tesla_ro_sid: str | None = Cookie(None)):
        conv = conversation(tesla_ro_sid, response)
        if conv.session.order is None:
            raise HTTPException(404, "Заказ-наряд ещё не создан")
        return render_html(conv.session.order, catalog.service)

    @app.get("/print/{number}", response_class=HTMLResponse)
    def print_saved(number: str):
        try:
            return render_html(store.load(number), catalog.service)
        except FileNotFoundError:
            raise HTTPException(404, "Заказ-наряд не найден") from None

    @app.exception_handler(HTTPException)
    def http_error(_, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description="Веб-интерфейс помощника по заказ-нарядам Tesla")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--orders-dir", default="orders")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    args = parser.parse_args(argv)
    app = create_app(args.catalog, args.orders_dir, model=args.model, effort=args.effort)
    print(f"Откройте в браузере: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
