"""Друкована форма замовлення-наряду (HTML)."""

from __future__ import annotations

from decimal import Decimal
from html import escape

from .order import RepairOrder


def fmt(value: Decimal | float) -> str:
    return f"{Decimal(str(value)):,.2f}".replace(",", " ").replace(".", ",")


def _num(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def render_html(order: RepairOrder, service: dict | None = None) -> str:
    service = service or {}
    e = lambda s: escape(str(s)) if s not in (None, "") else "—"  # noqa: E731
    c, v, t = order.customer, order.vehicle, order.totals()
    cur = escape(order.currency)

    labor_rows = "".join(
        f"<tr><td>{i}</td><td>{e(l.code)}</td><td>{e(l.name)}</td><td class=r>{_num(l.hours)}</td>"
        f"<td class=r>{fmt(l.rate)}</td><td class=r>{_num(l.discount_percent)}</td><td class=r>{fmt(l.total)}</td></tr>"
        for i, l in enumerate(order.labor, 1)
    ) or "<tr><td colspan=7>—</td></tr>"
    part_rows = "".join(
        f"<tr><td>{i}</td><td>{e(p.part_no)}</td><td>{e(p.name)}</td><td class=r>{_num(p.qty)} {e(p.unit)}</td>"
        f"<td class=r>{fmt(p.price)}</td><td class=r>{_num(p.discount_percent)}</td><td class=r>{fmt(p.total)}</td></tr>"
        for i, p in enumerate(order.parts, 1)
    ) or "<tr><td colspan=7>—</td></tr>"
    complaints = "".join(f"<li>{e(x)}</li>" for x in order.complaints) or "<li>—</li>"
    recs = "".join(f"<li>{e(x)}</li>" for x in order.recommendations)
    # Реквізити сервісу: показуємо лише заповнені поля.
    contacts = " · ".join(
        label + escape(str(service[key])) for key, label in (("phone", "тел. "), ("inn", "ЄДРПОУ ")) if service.get(key)
    )
    service_block = "<br>".join(
        x for x in (
            f"<b>{escape(str(service['name']))}</b>" if service.get("name") else "",
            escape(str(service["address"])) if service.get("address") else "",
            contacts,
        ) if x
    )
    vat_label = (
        f"у т.ч. ПДВ {_num(order.vat_percent)}%" if order.prices_include_vat else f"ПДВ {_num(order.vat_percent)}%"
    )

    return f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8">
<title>Замовлення-наряд {e(order.number)}</title>
<style>
body{{font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#111;margin:24px;}}
h1{{font-size:18px;margin:0 0 4px}} h2{{font-size:13px;margin:16px 0 6px;border-bottom:1px solid #999}}
table{{width:100%;border-collapse:collapse}} td,th{{border:1px solid #bbb;padding:4px 6px;vertical-align:top}}
th{{background:#eee;text-align:left}} .r{{text-align:right;white-space:nowrap}}
.grid td{{border:none;padding:2px 6px}} .grid td:nth-child(odd){{color:#555;width:16%}}
.head{{display:flex;justify-content:space-between}} .tot td{{border:none}} .sign{{margin-top:40px;display:flex;gap:60px}}
.sign div{{flex:1;border-top:1px solid #000;padding-top:4px}}
@media print{{body{{margin:0}}}}
</style></head><body>
<div class=head><div><h1>ЗАМОВЛЕННЯ-НАРЯД № {e(order.number)}</h1>
<div>від {e(order.created_at)} · статус: {e(order.status)}</div></div>
<div style="text-align:right">{service_block}</div></div>

<h2>Замовник і автомобіль</h2>
<table class=grid>
<tr><td>Замовник</td><td>{e(c.name)}</td><td>Автомобіль</td><td>Tesla {e(v.model)} {e(v.year)}</td></tr>
<tr><td>Телефон</td><td>{e(c.phone)}</td><td>VIN</td><td>{e(v.vin)}</td></tr>
<tr><td>E-mail</td><td>{e(c.email)}</td><td>Держ. номер</td><td>{e(v.plate)}</td></tr>
<tr><td>Колір</td><td>{e(v.color)}</td><td>Пробіг, км</td><td>{e(v.odometer_km)}</td></tr>
<tr><td></td><td></td><td>Заряд ВВБ, %</td><td>{e(v.battery_percent)}</td></tr>
</table>

<h2>Причина звернення (зі слів замовника)</h2><ul>{complaints}</ul>
{f"<h2>Результати діагностики</h2><p>{e(order.diagnosis)}</p>" if order.diagnosis else ""}

<h2>Роботи</h2>
<table><tr><th>№</th><th>Код</th><th>Найменування</th><th class=r>Н/год</th><th class=r>Ціна н/год, {cur}</th><th class=r>Знижка, %</th><th class=r>Сума, {cur}</th></tr>
{labor_rows}</table>

<h2>Запасні частини та матеріали</h2>
<table><tr><th>№</th><th>Артикул</th><th>Найменування</th><th class=r>Кількість</th><th class=r>Ціна, {cur}</th><th class=r>Знижка, %</th><th class=r>Сума, {cur}</th></tr>
{part_rows}</table>

<table class=tot style="width:auto;margin:12px 0 0 auto">
<tr><td>Разом роботи:</td><td class=r>{fmt(t["labor"])} {cur}</td></tr>
<tr><td>Разом запчастини:</td><td class=r>{fmt(t["parts"])} {cur}</td></tr>
{f'<tr><td>{vat_label}:</td><td class=r>{fmt(t["vat"])} {cur}</td></tr>' if order.vat_percent else ""}
<tr><td><b>УСЬОГО ДО СПЛАТИ:</b></td><td class=r><b>{fmt(t["total"])} {cur}</b></td></tr>
</table>
{f"<h2>Рекомендації</h2><ul>{recs}</ul>" if recs else ""}

<p style="margin-top:16px;font-size:11px;color:#444">З обсягом і вартістю робіт ознайомлений і згоден.
Роботи з високовольтною системою виконує кваліфікований персонал із дотриманням вимог безпеки.</p>
<div class=sign><div>Майстер-приймальник</div><div>Замовник</div></div>
</body></html>
"""
