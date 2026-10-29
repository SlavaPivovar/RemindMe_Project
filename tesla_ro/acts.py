"""Акт приймання автомобіля: що залишили в авто, наявні пошкодження, фото й підпис клієнта.

Дані акта лежать у замовленні-наряді (поле intake), підпис — PNG у orders/signatures/.
Будь-яка зміна змісту акта після підпису знімає підпис: клієнт підписував інший текст.
"""

from __future__ import annotations

import base64
import secrets
from html import escape
from pathlib import Path

from .order import NUMBER_RE, RepairOrder, now_str

INTAKE_ITEMS = ("Ключ-карта", "Брелок-ключ", "Зарядний кабель (Mobile Connector)", "Адаптер для зарядки",
                "Ремкомплект / компресор для шин", "Килимки", "Дитяче крісло", "Документи на автомобіль")
MAX_TEXT = 1000
MAX_SIGNATURE = 600 * 1024
CONSENT = ("Із технічним станом автомобіля, переліком залишених речей і зауваженнями щодо пошкоджень "
           "ознайомлений(-а) і згоден(-на). Автомобіль передано на сервіс для виконання робіт за замовленням-нарядом.")


class ActError(ValueError):
    """Текст показується користувачу."""


def clean_intake(data: dict) -> dict:
    items = data.get("items") or []
    if not isinstance(items, list):
        raise ActError("Перелік речей має бути списком")
    items = list(dict.fromkeys(" ".join(str(x).split())[:80] for x in items if str(x).strip()))[:30]
    result = {"items": items}
    for key in ("left_inside", "damages"):
        value = str(data.get(key) or "").strip()
        if len(value) > MAX_TEXT:
            raise ActError(f"Текст задовгий (до {MAX_TEXT} символів)")
        result[key] = value
    return result


def update_intake(order: RepairOrder, data: dict) -> bool:
    """Записати зміст акта. Повертає True, якщо підпис знято через зміну змісту."""
    new = clean_intake(data)
    old = {k: order.intake.get(k, [] if k == "items" else "") for k in new}
    if new == old:
        return False
    unsigned = bool(order.intake.get("signature"))
    order.intake = {**new, "updated_at": now_str()}  # підпис і його дата скидаються
    return unsigned


class Signatures:
    def __init__(self, orders_dir: str | Path):
        self.dir = Path(orders_dir) / "signatures"

    def save(self, number: str, data: bytes) -> str:
        if not NUMBER_RE.match(number):
            raise ActError("Некоректний номер замовлення-наряду")
        if not data.startswith(b"\x89PNG") or len(data) > MAX_SIGNATURE:
            raise ActError("Підпис має бути зображенням PNG до 600 КБ")
        self.dir.mkdir(parents=True, exist_ok=True)
        name = f"{number}-{secrets.token_hex(4)}.png"
        (self.dir / name).write_bytes(data)
        return name

    def path(self, name: str) -> Path:
        path = self.dir / Path(name).name
        if not name or Path(name).name != name or not path.exists():
            raise FileNotFoundError(name)
        return path


def _data_uri(path: Path, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def render_act_html(order: RepairOrder, service: dict | None = None, photos: list[tuple[dict, Path]] = (),
                    signature: Path | None = None) -> str:
    service = service or {}
    e = lambda s: escape(str(s)) if s not in (None, "") else "—"  # noqa: E731
    c, v, act = order.customer, order.vehicle, order.intake
    items = act.get("items", [])
    extra = [x for x in items if x not in INTAKE_ITEMS]
    checklist = "".join(f"<li>{'☑' if x in items else '☐'} {escape(x)}</li>" for x in (*INTAKE_ITEMS, *extra))
    mime = {".png": "image/png", ".webp": "image/webp"}
    pics = "".join(
        f"<figure><img src='{_data_uri(p, mime.get(p.suffix, 'image/jpeg'))}'><figcaption>{escape(meta.get('kind', ''))}"
        f" · {escape(meta.get('at', ''))}</figcaption></figure>"
        for meta, p in photos if p.exists())
    head = "<br>".join(x for x in (
        f"<b>{escape(str(service['name']))}</b>" if service.get("name") else "",
        escape(str(service["address"])) if service.get("address") else "") if x)
    sign = f"<img class=sig src='{_data_uri(signature, 'image/png')}'>" if signature else "<div class=blank></div>"
    signer = f" — {e(act.get('signed_by'))}, {e(act.get('signed_at'))}" if signature else ""
    return f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><title>Акт приймання {e(order.number)}</title>
<style>
body{{font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#111;margin:24px}}
h1{{font-size:17px;margin:0 0 4px}} h2{{font-size:13px;margin:14px 0 6px;border-bottom:1px solid #999}}
.head{{display:flex;justify-content:space-between}} table{{width:100%;border-collapse:collapse}}
td{{padding:2px 6px;vertical-align:top}} td:nth-child(odd){{color:#555;width:16%}}
ul{{margin:4px 0;padding-left:0;list-style:none;columns:2}} li{{margin:2px 0}}
.photos{{display:flex;flex-wrap:wrap;gap:6px}} figure{{margin:0;width:31%}}
figure img{{width:100%;height:120px;object-fit:cover;border:1px solid #ccc}} figcaption{{font-size:10px;color:#555}}
.signs{{display:flex;gap:40px;margin-top:22px}} .signs>div{{flex:1}} .sig{{height:70px;max-width:100%;display:block}}
.blank{{height:70px}} .line{{border-top:1px solid #000;padding-top:3px}}
@page{{size:A4;margin:14mm}}
</style></head><body>
<div class=head><div><h1>АКТ ПРИЙМАННЯ АВТОМОБІЛЯ</h1>
<div>до замовлення-наряду № {e(order.number)} від {e(order.created_at)}</div></div>
<div style="text-align:right">{head}</div></div>
<h2>Замовник і автомобіль</h2>
<table>
<tr><td>Замовник</td><td>{e(c.name)}</td><td>Автомобіль</td><td>Tesla {e(v.model)} {e(v.year)}</td></tr>
<tr><td>Телефон</td><td>{e(c.phone)}</td><td>VIN</td><td>{e(v.vin)}</td></tr>
<tr><td>Колір</td><td>{e(v.color)}</td><td>Держ. номер</td><td>{e(v.plate)}</td></tr>
<tr><td>Пробіг, км</td><td>{e(v.odometer_km)}</td><td>Заряд ВВБ, %</td><td>{e(v.battery_percent)}</td></tr>
</table>
<h2>Комплектність і речі</h2><ul>{checklist}</ul>
<p><b>Залишено в салоні / багажнику:</b> {e(act.get('left_inside'))}</p>
<h2>Наявні пошкодження та зауваження</h2><p>{e(act.get('damages')).replace(chr(10), '<br>')}</p>
{f"<h2>Фото під час приймання</h2><div class=photos>{pics}</div>" if pics else ""}
<p style="margin-top:14px;font-size:11px">{CONSENT}</p>
<div class=signs>
<div><div class=blank></div><div class=line>Майстер-приймальник</div></div>
<div>{sign}<div class=line>Замовник (підпис){signer}</div></div>
</div>
</body></html>"""


def render_act_pdf(*args, **kwargs) -> bytes:
    from .render import PdfUnavailable

    try:
        import weasyprint
    except (ImportError, OSError) as exc:
        raise PdfUnavailable(str(exc)) from None
    return weasyprint.HTML(string=render_act_html(*args, **kwargs)).write_pdf()
