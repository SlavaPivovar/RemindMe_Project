"""Резервна копія даних сервісу одним ZIP-архівом.

У копію входять замовлення-наряди (разом із кошиком, коментарями й фото), довідник, журнал
витрат на Claude і зв'язки Telegram-бота. Ключ підпису входу (.auth_secret) не входить: копія
надсилається в Telegram, а ключ має лишатися тільки на сервері. Друковані HTML-форми теж не
потрібні — їх завжди можна згенерувати знову.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime
from pathlib import Path

TELEGRAM_LIMIT = 45 * 1024 * 1024  # Bot API приймає документи до 50 МБ


def _files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix not in (".tmp", ".html"))


def make_backup(orders_dir: Path, extra_files: list[Path], max_bytes: int | None = None) -> tuple[bytes, dict]:
    """Зібрати архів. Якщо з фото він перевищить max_bytes — фото не додаються (про це каже info)."""
    orders_dir = Path(orders_dir)
    photos_dir = orders_dir / "photos"
    regular = [p for p in _files(orders_dir) if photos_dir not in p.parents]
    photos = _files(photos_dir)

    def build(include_photos: bool) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in regular:
                zf.write(p, "orders/" + p.relative_to(orders_dir).as_posix())
            for p in extra_files:
                if p and Path(p).exists():
                    zf.write(p, Path(p).name)
            if include_photos:
                for p in photos:
                    # JPEG і так стиснений — не витрачаємо час на повторне стискання
                    zf.write(p, "orders/" + p.relative_to(orders_dir).as_posix(), compress_type=zipfile.ZIP_STORED)
        return buf.getvalue()

    photos_size = sum(p.stat().st_size for p in photos)
    data = build(include_photos=False)
    with_photos = bool(photos) and (max_bytes is None or len(data) + photos_size <= max_bytes)
    if with_photos:
        data = build(include_photos=True)
    orders = sum(1 for p in regular if p.parent == orders_dir and p.suffix == ".json")
    info = {"orders": orders, "photos": len(photos) if with_photos else 0,
            "photos_skipped": len(photos) if photos and not with_photos else 0, "size": len(data)}
    return data, info


def backup_filename(service_name: str = "") -> str:
    prefix = "".join(ch if ch.isalnum() else "-" for ch in service_name).strip("-").lower() or "service"
    return f"{prefix}-backup-{datetime.now():%Y-%m-%d}.zip"
