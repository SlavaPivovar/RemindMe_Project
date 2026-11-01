"""Співробітники: ролі, вхід кожного під своїм ім'ям, прив'язка до Telegram.

Ролі: власник (усе, зокрема співробітники, налаштування, резервні копії, витрати на Claude),
адміністратор (плюс оплати й каса, видалення, довідник, склад і закупівлі, розсилки) і майстер
(замовлення, статуси, коментарі, фото, запис).

Увійти можна кодом, який бот надсилає співробітнику в Telegram (після прив'язки за
посиланням-запрошенням), або особистим паролем, який задає власник. Загальний пароль сервісу
(APP_PASSWORD) лишається входом власника — щоб не можна було замкнути себе ззовні.
Список зберігається в staff.json поруч із каталогом orders/.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

from .clients import phone_key
from .order import now_str

OWNER, ADMIN, MASTER = "owner", "admin", "master"
ROLES = {OWNER: "власник", ADMIN: "адміністратор", MASTER: "майстер"}
RANK = {MASTER: 1, ADMIN: 2, OWNER: 3}
CODE_TTL = 5 * 60
CODE_ATTEMPTS = 5
CODE_EVERY = 45  # не частіше ніж раз на 45 с на один номер


class StaffError(ValueError):
    """Текст показується користувачу."""


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return f"pbkdf2${salt}${digest}"


def check_password(stored: str, password: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
    except (AttributeError, ValueError):
        return False
    probe = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return hmac.compare_digest(probe, digest)


def public(e: dict) -> dict:
    """Без хешу пароля — для інтерфейсу."""
    return {k: v for k, v in e.items() if k != "pwd"} | {"has_password": bool(e.get("pwd")),
                                                         "role_label": ROLES.get(e.get("role"), "")}


class StaffStore:
    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._memory: list[dict] = []
        self._codes: dict[str, dict] = {}  # ключ телефону -> {code, until, tries, id, sent}

    # --- файл ---

    def _read(self) -> list[dict]:
        if self.path is None:
            return self._memory
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("employees", [])
        except (FileNotFoundError, ValueError):
            return []

    def _write(self, items: list[dict]) -> None:
        if self.path is None:
            self._memory = items
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps({"employees": items}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    # --- читання ---

    def list(self) -> list[dict]:
        return self._read()

    def get(self, emp_id: str) -> dict | None:
        return next((e for e in self._read() if e["id"] == emp_id), None)

    def by_phone(self, phone: str) -> dict | None:
        key = phone_key(phone or "")
        return next((e for e in self._read() if key and phone_key(e["phone"]) == key), None) if key else None

    def by_tg(self, tg_id) -> dict | None:
        return next((e for e in self._read() if tg_id and e.get("tg_id") == tg_id), None)

    def telegram_bound(self) -> bool:
        """Чи хтось уже прив'язав Telegram — тоді в групі працюють лише співробітники."""
        return any(e.get("tg_id") for e in self._read())

    # --- зміни ---

    @staticmethod
    def _clean(data: dict, partial: bool) -> dict:
        out = {}
        if "name" in data or not partial:
            name = " ".join(str(data.get("name") or "").split())[:80]
            if not name:
                raise StaffError("Вкажіть ім'я")
            out["name"] = name
        if "phone" in data or not partial:
            phone = str(data.get("phone") or "").strip()[:30]
            if not phone_key(phone):
                raise StaffError("Вкажіть телефон (за ним співробітник входить)")
            out["phone"] = phone
        if "role" in data or not partial:
            if data.get("role") not in ROLES:
                raise StaffError("Невідома роль")
            out["role"] = data["role"]
        if "active" in data:
            out["active"] = bool(data["active"])
        return out

    def create(self, data: dict) -> dict:
        e = {"id": secrets.token_hex(4), **self._clean(data, partial=False), "active": True, "tg_id": None,
             "tg_name": "", "invite": "s_" + secrets.token_urlsafe(12), "pwd": "", "ver": 1, "created_at": now_str()}
        with self._lock:
            items = self._read()
            if self.by_phone(e["phone"]):
                raise StaffError("Співробітник із таким телефоном уже є")
            items.append(e)
            self._write(items)
        return e

    def update(self, emp_id: str, data: dict) -> dict:
        changes = self._clean(data, partial=True)
        with self._lock:
            items = self._read()
            e = next((x for x in items if x["id"] == emp_id), None)
            if e is None:
                raise KeyError(emp_id)
            if "phone" in changes:
                other = self.by_phone(changes["phone"])
                if other and other["id"] != emp_id:
                    raise StaffError("Співробітник із таким телефоном уже є")
            if (changes.get("active") is False and e["active"]) or changes.get("role", e["role"]) != e["role"]:
                e["ver"] = e.get("ver", 1) + 1  # вийти з усіх пристроїв: права змінились
            e.update(changes)
            self._write(items)
        return e

    def set_password(self, emp_id: str, password: str | None) -> dict:
        if password is not None and len(password) < 8:
            raise StaffError("Пароль — щонайменше 8 символів")
        with self._lock:
            items = self._read()
            e = next((x for x in items if x["id"] == emp_id), None)
            if e is None:
                raise KeyError(emp_id)
            e["pwd"] = hash_password(password) if password else ""
            e["ver"] = e.get("ver", 1) + 1  # старі входи з іншим паролем — недійсні
            self._write(items)
        return e

    def new_invite(self, emp_id: str) -> dict:
        """Нове посилання-запрошення; старе перестає діяти, прив'язаний Telegram відв'язується."""
        with self._lock:
            items = self._read()
            e = next((x for x in items if x["id"] == emp_id), None)
            if e is None:
                raise KeyError(emp_id)
            e["invite"], e["tg_id"], e["tg_name"] = "s_" + secrets.token_urlsafe(12), None, ""
            self._write(items)
        return e

    def bind_telegram(self, invite: str, tg_id: int, tg_name: str) -> dict | None:
        with self._lock:
            items = self._read()
            e = next((x for x in items if invite and x.get("invite") == invite), None)
            if e is None or not e["active"]:
                return None
            for other in items:  # один Telegram — один співробітник
                if other is not e and other.get("tg_id") == tg_id:
                    other["tg_id"], other["tg_name"] = None, ""
            e["tg_id"], e["tg_name"], e["invite"] = tg_id, tg_name[:80], ""  # запрошення одноразове
            self._write(items)
            return e

    # --- вхід ---

    def login_password(self, phone: str, password: str) -> dict | None:
        e = self.by_phone(phone)
        if e and e["active"] and e.get("pwd") and check_password(e["pwd"], password):
            return e
        return None

    def request_code(self, phone: str) -> tuple[dict, str]:
        """Новий код для входу. Повертає (співробітник, код) — код надсилає бот."""
        e = self.by_phone(phone)
        if e is None or not e["active"]:
            raise StaffError("Немає активного співробітника з таким телефоном")
        if not e.get("tg_id"):
            raise StaffError("Telegram ще не прив'язано — відкрийте посилання-запрошення від власника або увійдіть паролем")
        key = phone_key(phone)
        now = time.monotonic()
        with self._lock:
            prev = self._codes.get(key)
            if prev and now - prev["sent"] < CODE_EVERY:
                raise StaffError("Код уже надіслано — зачекайте хвилину")
            code = f"{secrets.randbelow(1_000_000):06d}"
            self._codes[key] = {"code": code, "until": now + CODE_TTL, "tries": 0, "id": e["id"], "sent": now}
        return e, code

    def verify_code(self, phone: str, code: str) -> dict | None:
        key = phone_key(phone or "")
        with self._lock:
            entry = self._codes.get(key) if key else None
            if entry is None or time.monotonic() > entry["until"]:
                self._codes.pop(key, None)
                return None
            entry["tries"] += 1
            if not hmac.compare_digest(entry["code"], (code or "").strip()):
                if entry["tries"] >= CODE_ATTEMPTS:
                    self._codes.pop(key, None)  # забагато спроб — потрібен новий код
                return None
            self._codes.pop(key, None)
        e = self.get(entry["id"])
        return e if e and e["active"] else None
