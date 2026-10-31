"""Фото до замовлень-нарядів: «до», «після», пошкодження.

Фото лежать окремо від JSON замовлення (orders/photos/<номер>/), щоб автосохранення замовлення
й фото з Telegram не перезаписували одне одного. Опис фото — в index.json у тій самій папці.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path

from .order import NUMBER_RE, now_str

KINDS = ("до", "після", "пошкодження", "інше")
EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
MAX_PHOTOS = 60
MAX_BYTES = 8 * 1024 * 1024


class PhotoError(ValueError):
    """Текст показується користувачу."""


class PhotoStore:
    def __init__(self, orders_dir: str | Path):
        self.root = Path(orders_dir) / "photos"
        self._lock = threading.Lock()

    def _dir(self, number: str) -> Path:
        if not NUMBER_RE.match(number):
            raise FileNotFoundError(number)
        return self.root / number

    def _read(self, number: str) -> list[dict]:
        try:
            return json.loads((self._dir(number) / "index.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return []

    def _write(self, number: str, items: list[dict]) -> None:
        d = self._dir(number)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"index.{threading.get_ident()}.tmp"
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, d / "index.json")

    def list(self, number: str) -> list[dict]:
        return self._read(number)

    def add(self, number: str, data: bytes, media_type: str, kind: str = "до", author: str = "",
            source: str = "web") -> dict:
        ext = EXTENSIONS.get(media_type)
        if ext is None:
            raise PhotoError("Підтримуються фото JPEG, PNG або WebP")
        if not data or len(data) > MAX_BYTES:
            raise PhotoError("Фото завелике (до 8 МБ)")
        kind = kind if kind in KINDS else "інше"
        with self._lock:
            items = self._read(number)
            if len(items) >= MAX_PHOTOS:
                raise PhotoError(f"До одного замовлення можна додати до {MAX_PHOTOS} фото")
            photo_id = secrets.token_hex(6)
            d = self._dir(number)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{photo_id}{ext}").write_bytes(data)
            item = {"id": photo_id, "file": f"{photo_id}{ext}", "kind": kind, "to_client": False,
                    "at": now_str(), "author": author[:100], "source": source}
            items.append(item)
            self._write(number, items)
        return item

    def update(self, number: str, photo_id: str, kind: str | None = None, to_client: bool | None = None,
               line: str | None = None) -> dict:
        with self._lock:
            items = self._read(number)
            item = next((x for x in items if x["id"] == photo_id), None)
            if item is None:
                raise KeyError(photo_id)
            if kind is not None:
                if kind not in KINDS:
                    raise PhotoError(f"Невідомий тип фото. Можливі: {', '.join(KINDS)}")
                item["kind"] = kind
            if to_client is not None:
                item["to_client"] = bool(to_client)
            if line is not None:
                item["line"] = line.strip()[:200]  # «» — не прив'язано до рядка
            self._write(number, items)
        return item

    def delete(self, number: str, photo_id: str) -> None:
        with self._lock:
            items = self._read(number)
            item = next((x for x in items if x["id"] == photo_id), None)
            if item is None:
                raise KeyError(photo_id)
            self._write(number, [x for x in items if x["id"] != photo_id])
            (self._dir(number) / item["file"]).unlink(missing_ok=True)

    def path(self, number: str, photo_id: str) -> Path:
        item = next((x for x in self._read(number) if x["id"] == photo_id), None)
        if item is None:
            raise KeyError(photo_id)
        return self._dir(number) / item["file"]

    def dir_path(self, number: str, item: dict) -> Path:
        return self._dir(number) / item["file"]

    def for_client(self, number: str) -> list[tuple[dict, Path]]:
        return [(x, self._dir(number) / x["file"]) for x in self._read(number) if x.get("to_client")]
