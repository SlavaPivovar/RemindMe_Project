"""Внутрішні коментарі співробітників до замовлень-нарядів (не друкуються).

Коментарі лежать окремо від замовлення (orders/comments/<номер>.jsonl, один рядок — один коментар):
так коментар із Telegram не затре автозбереження замовлення, відкритого в когось у браузері.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path

from .order import NUMBER_RE

MAX_TEXT = 2000
MAX_VOICE = 20 * 1024 * 1024  # більші файли Bot API однаково не віддає
VOICE_FILE = re.compile(r"^[0-9a-f]{12}\.(ogg|oga|mp3|m4a)$")


class CommentError(ValueError):
    """Коментар не можна додати; текст повідомлення показується користувачу."""


class CommentStore:
    def __init__(self, orders_dir: str | Path):
        self.dir = Path(orders_dir) / "comments"
        self._lock = threading.Lock()

    def _path(self, number: str) -> Path:
        if not NUMBER_RE.match(number):
            raise CommentError("Некоректний номер замовлення-наряду")
        return self.dir / f"{number}.jsonl"

    def add(self, number: str, author: str, text: str, source: str = "web", voice: dict | None = None) -> dict:
        """voice — голосовий коментар: {"file": ім'я з save_voice, "duration": секунд}."""
        text = text.strip()
        if not text:
            raise CommentError("Порожній коментар")
        if len(text) > MAX_TEXT:
            raise CommentError(f"Коментар задовгий (до {MAX_TEXT} символів)")
        comment = {"at": datetime.now().strftime("%d.%m.%Y %H:%M"), "author": author.strip()[:80] or "Співробітник",
                   "text": text, "source": source, **({"voice": voice} if voice else {})}
        path = self._path(number)
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(comment, ensure_ascii=False) + "\n")
        return comment

    def save_voice(self, number: str, data: bytes, ext: str = "ogg") -> str:
        if not data or len(data) > MAX_VOICE:
            raise CommentError("Голосове повідомлення завелике")
        name = f"{secrets.token_hex(6)}.{ext}"
        folder = self.dir / "voice" / self._path(number).stem
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
        return name

    def voice_path(self, number: str, name: str) -> Path:
        if not VOICE_FILE.match(name):
            raise FileNotFoundError(name)
        path = self.dir / "voice" / self._path(number).stem / name
        if not path.exists():
            raise FileNotFoundError(name)
        return path

    def list(self, number: str) -> list[dict]:
        try:
            path = self._path(number)
        except CommentError:
            return []
        if not path.exists():
            return []
        result = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return result

    def count(self, number: str) -> int:
        return len(self.list(number))
