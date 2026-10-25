"""Розпізнавання VIN і пробігу з фото (Claude vision)."""

from __future__ import annotations

import base64
import binascii
import json
import re

import anthropic

from .vin import decode_vin

MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # обмеження API на одне зображення
MILES_TO_KM = 1.609344

PROMPT = """\
На фото — автомобіль Tesla або його частина. Знайди:
- VIN (17 символів): табличка під лобовим склом, наклейка на стійці дверей, екран
  «Програмне забезпечення» / «Software» на дисплеї, застосунок Tesla, документи;
- показання одометра (пробіг) на дисплеї або в документах та одиниці виміру (км чи милі).
Переписуй символи VIN точно як на фото. Якщо якийсь символ нечіткий, усе одно дай найімовірніший
варіант і вкажи це в notes. Якщо чогось на фото немає — став null. notes — коротко українською."""

SCHEMA = {
    "type": "object",
    "properties": {
        "vin": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "odometer_value": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "odometer_unit": {"type": "string", "enum": ["km", "mi", "unknown"]},
        "notes": {"type": "string"},
    },
    "required": ["vin", "odometer_value", "odometer_unit", "notes"],
    "additionalProperties": False,
}


class PhotoError(ValueError):
    """Фото не вдалося обробити; текст повідомлення показується користувачу."""


def parse_data_url(data_url: str) -> tuple[str, str]:
    """data:image/jpeg;base64,... -> (media_type, base64) з перевіркою типу й розміру."""
    match = re.fullmatch(r"data:([\w/+.-]+);base64,(.+)", data_url.strip(), re.S)
    if not match:
        raise PhotoError("Некоректне зображення")
    media_type, data = match.group(1).lower(), re.sub(r"\s", "", match.group(2))
    if media_type not in MEDIA_TYPES:
        raise PhotoError("Підтримуються фото JPEG, PNG, WebP або GIF")
    try:
        size = len(base64.b64decode(data, validate=True))
    except (binascii.Error, ValueError):
        raise PhotoError("Некоректне зображення") from None
    if size > MAX_IMAGE_BYTES:
        raise PhotoError("Фото завелике (понад 5 МБ)")
    return media_type, data


def clean_vin(raw: str | None) -> str | None:
    """Прибрати пробіли й дефіси; I, O, Q у VIN не бувають — це майже завжди 1, 0, 0."""
    if not raw:
        return None
    vin = re.sub(r"[\s\-–—.]", "", raw.upper())
    return vin.translate(str.maketrans({"I": "1", "O": "0", "Q": "0"})) or None


def recognize(client: anthropic.Anthropic, model: str, media_type: str, data: str) -> dict:
    response = client.beta.messages.create(
        model=model,
        max_tokens=4000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
            {"type": "text", "text": PROMPT},
        ]}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        # У разі відмови класифікатора запит повторюється на рекомендованій моделі.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise PhotoError("Модель відмовилася обробити це фото")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        raise PhotoError("Не вдалося розпізнати фото, спробуйте ще раз") from None
    return summarize(raw)


def summarize(raw: dict) -> dict:
    """Перевірити розпізнане й скласти текст для поля введення."""
    vin = clean_vin(raw.get("vin"))
    info = decode_vin(vin) if vin else None
    value, unit = raw.get("odometer_value"), raw.get("odometer_unit", "unknown")
    odometer_km = None
    if isinstance(value, (int, float)) and value >= 0:
        odometer_km = round(value * MILES_TO_KM) if unit == "mi" else round(value)

    parts, warnings = [], []
    if info:
        car = " ".join(str(x) for x in (info.model_name, info.year) if x)
        parts.append(f"VIN {vin}" + (f" ({car})" if car else ""))
        warnings += info.warnings
    if odometer_km is not None:
        if unit == "mi":
            parts.append(f"пробіг {odometer_km:,} км (на фото {value:,.0f} миль)".replace(",", " "))
        else:
            parts.append(f"пробіг {odometer_km:,} км".replace(",", " "))
            if unit == "unknown":
                warnings.append("не видно, км це чи милі — перевірте пробіг")
    notes = (raw.get("notes") or "").strip()
    text = ("З фото: " + ", ".join(parts) + ".") if parts else ""
    return {
        "vin": vin, "vin_warnings": info.warnings if info else [],
        "odometer_km": odometer_km, "odometer_value": value, "odometer_unit": unit,
        "notes": notes, "warnings": warnings, "text": text,
    }
