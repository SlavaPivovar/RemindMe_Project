"""Погодження кошторису клієнтом — по кожному рядку — і посилання на сторінку замовлення для клієнта.

Кошторис фіксується на момент надсилання (рядки й суми), клієнт для кожного рядка вибирає
«погоджую» або «пізніше». Відповісти можна в Telegram (кнопки «все погоджую / відмовляюсь»)
або на сторінці замовлення за посиланням — зокрема без Telegram (посилання можна надіслати у
Viber чи SMS). Зберігається окремо від замовлення: orders/approvals/<номер>.json, а посилання —
orders/approvals/tokens.json.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from decimal import Decimal
from pathlib import Path

from .order import NUMBER_RE, RepairOrder, money, now_str

PENDING, APPROVED, PARTIAL, DECLINED = "pending", "approved", "partial", "declined"
YES, LATER = "approved", "deferred"


class ApprovalError(ValueError):
    """Текст показується клієнту чи майстру."""


def line_key(kind: str, index: int) -> str:
    return f"{kind}:{index}"


def summary(rec: dict) -> str:
    """«погодив 2 з 3 на 222,00 $, відклав: Диски (180,00 $)» — для коментарів і повідомлень."""
    def m(v) -> str:
        return f"{Decimal(v):,.2f}".replace(",", " ").replace(".", ",")
    cur = rec.get("currency", "$")
    lines = rec.get("lines", [])
    if rec["status"] == DECLINED:
        return f"відмовився від кошторису на {m(rec['total'])} {cur}"
    if not lines or rec["status"] == APPROVED:
        return f"погодив кошторис на {m(rec.get('approved_total') or rec['total'])} {cur}"
    yes = [x for x in lines if x["decision"] == YES]
    later = [x for x in lines if x["decision"] == LATER]
    text = f"погодив {len(yes)} з {len(lines)} на {m(rec['approved_total'])} {cur}"
    if later:
        text += ", відклав: " + "; ".join(f"{x['name']} ({m(x['total'])} {cur})" for x in later)
    return text


class ApprovalStore:
    def __init__(self, orders_dir: str | Path):
        self.dir = Path(orders_dir) / "approvals"
        self._lock = threading.RLock()

    # --- файли ---

    def _path(self, number: str) -> Path:
        if not NUMBER_RE.match(number):
            raise FileNotFoundError(number)
        return self.dir / f"{number}.json"

    def _write(self, path: Path, data) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def get(self, number: str) -> dict | None:
        try:
            return json.loads(self._path(number).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    def all(self) -> dict[str, dict]:
        result = {}
        for p in sorted(self.dir.glob("ЗН-*.json")) if self.dir.exists() else []:
            try:
                result[p.stem] = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                continue
        return result

    # --- погодження ---

    def create(self, order: RepairOrder, via: str = "web") -> dict:
        """Зафіксувати кошторис і почати погодження (попереднє, якщо було, замінюється)."""
        lines = [{"key": line_key("labor", i), "kind": "labor", "name": x.name, "qty": x.qty, "total": str(x.total),
                  "decision": PENDING} for i, x in enumerate(order.labor)]
        lines += [{"key": line_key("parts", i), "kind": "parts", "name": x.name, "qty": x.qty, "total": str(x.total),
                   "decision": PENDING} for i, x in enumerate(order.parts)]
        if not lines:
            raise ApprovalError("У кошторисі ще немає робіт і запчастин")
        rec = {"status": PENDING, "total": str(order.totals()["total"]), "currency": order.currency,
               "sent_at": now_str(), "sent_via": via, "at": "", "via": "", "by": "", "lines": lines,
               "approved_total": "0.00", "deferred_total": "0.00", "chat_id": None, "message_id": None}
        with self._lock:
            self._write(self._path(order.number), rec)
        return rec

    def set_message(self, number: str, chat_id: int, message_id: int | None) -> None:
        with self._lock:
            rec = self.get(number)
            if rec:
                rec["chat_id"], rec["message_id"] = chat_id, message_id
                self._write(self._path(number), rec)

    def decide(self, number: str, decisions: dict[str, str], via: str, by: str = "") -> dict:
        """decisions — {ключ рядка: "approved" | "deferred"}; рядки без відповіді вважаються «пізніше»."""
        with self._lock:
            rec = self.get(number)
            if rec is None:
                raise ApprovalError("Кошторис не знайдено")
            if rec["status"] != PENDING:
                raise ApprovalError("Ви вже відповіли на цей кошторис.")
            if any(v not in (YES, LATER) for v in decisions.values()):
                raise ApprovalError("Невідома відповідь")
            for line in rec["lines"]:
                line["decision"] = decisions.get(line["key"], LATER)
            yes = [x for x in rec["lines"] if x["decision"] == YES]
            rec["approved_total"] = str(money(sum((Decimal(x["total"]) for x in yes), Decimal("0"))))
            rec["deferred_total"] = str(money(Decimal(rec["total"]) - Decimal(rec["approved_total"]))) \
                if rec["lines"] else "0.00"
            rec["status"] = APPROVED if len(yes) == len(rec["lines"]) else PARTIAL if yes else DECLINED
            rec["at"], rec["via"], rec["by"] = now_str(), via, by[:100]
            self._write(self._path(number), rec)
            return rec

    def decide_all(self, number: str, approve: bool, via: str, by: str = "") -> dict:
        rec = self.get(number)
        if rec is None:
            raise ApprovalError("Кошторис не знайдено")
        return self.decide(number, {x["key"]: YES if approve else LATER for x in rec["lines"]}, via, by)

    def import_legacy(self, number: str, old: dict) -> None:
        """Погодження зі старої версії (лише «все / нічого», без рядків) — з telegram.json."""
        if self.get(number) is not None:
            return
        status = {"approved": APPROVED, "declined": DECLINED}.get(old.get("status"), PENDING)
        rec = {"status": status, "total": old.get("total", "0"), "currency": old.get("currency", "$"),
               "sent_at": old.get("sent_at", ""), "sent_via": "telegram", "at": old.get("at", ""),
               "via": "telegram" if status != PENDING else "", "by": "", "lines": [],
               "approved_total": old.get("total", "0") if status == APPROVED else "0.00", "deferred_total": "0.00",
               "chat_id": old.get("chat_id"), "message_id": old.get("message_id")}
        with self._lock:
            self._write(self._path(number), rec)

    # --- посилання на сторінку клієнта ---

    def _tokens(self) -> dict:
        try:
            return json.loads((self.dir / "tokens.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {"tokens": {}, "orders": {}}

    def token_for(self, number: str) -> str:
        with self._lock:
            data = self._tokens()
            token = data["orders"].get(number)
            if token is None:
                token = secrets.token_urlsafe(16)  # не вгадати: на сторінці персональні дані
                data["orders"][number], data["tokens"][token] = token, number
                self._write(self.dir / "tokens.json", data)
            return token

    def number_for(self, token: str) -> str | None:
        return self._tokens()["tokens"].get(token)
