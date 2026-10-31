"""Сторінка замовлення-наряду для клієнта (за посиланням, без входу).

Показує статус ремонту, роботи й запчастини, фото, оплату, документи і — якщо кошторис чекає
відповіді — дає погодити його по пунктах. Внутрішнього (коментарі, витрати, відповідальний,
собівартість) тут немає.
"""

from __future__ import annotations

import json
from decimal import Decimal
from html import escape

from .fx import uah as to_uah
from .order import CANCELLED, DRAFT, RepairOrder

STEPS = (("прийнято", "Прийнято"), ("в роботі", "В роботі"), ("готово", "Готово"), ("видано", "Видано"))


def _m(v) -> str:
    return f"{Decimal(str(v)):,.2f}".replace(",", " ").replace(".", ",")


def render_client_page(order: RepairOrder, service: dict, token: str, rec: dict | None, photos: list[dict],
                       rate: dict | None = None, act: bool = False) -> str:
    e = lambda s: escape(str(s or ""))  # noqa: E731
    v, cur = order.vehicle, escape(order.currency)
    base = f"/c/{token}"
    car = " ".join(x for x in (f"Tesla {v.model}" if v.model else "", str(v.year or ""), v.plate) if x)
    in_uah = lambda amount: f' <span class="uah">≈ {_m(to_uah(amount, rate["rate"]))} грн</span>' if rate else ""  # noqa: E731

    # статус
    if order.status == CANCELLED:
        steps = '<div class="banner">Замовлення-наряд скасовано</div>'
    elif order.status == DRAFT:
        steps = '<div class="banner soft">Кошторис ще готується</div>'
    else:
        current = [s for s, _ in STEPS].index(order.status) if order.status in dict(STEPS) else -1
        steps = '<ol class="steps">' + "".join(
            f'<li class="{"done" if i < current else "now" if i == current else ""}">{label}</li>'
            for i, (_, label) in enumerate(STEPS)) + "</ol>"

    by_line: dict[str, list[dict]] = {}
    for p in photos:
        if p.get("line"):
            by_line.setdefault(p["line"], []).append(p)
    pic = lambda p: f'<a href="{base}/photo/{e(p["id"])}" target="_blank"><img loading="lazy" src="{base}/photo/{e(p["id"])}" alt="{e(p.get("kind"))}"></a>'  # noqa: E731

    # роботи й запчастини / погодження
    pending = rec is not None and rec.get("status") == "pending" and rec.get("lines")
    if pending:
        rows = "".join(f'''<div class="line" data-key="{e(x["key"])}" data-total="{e(x["total"])}">
            <div class="lt"><div><b>{e(x["name"])}</b>{f' <span class="muted">× {e(x["qty"])}</span>' if x.get("qty") not in (1, 1.0) else ""}</div>
              <div class="sum">{_m(x["total"])} {cur}</div></div>
            {f'<div class="pics">{"".join(pic(p) for p in by_line.get(x["name"], []))}</div>' if by_line.get(x["name"]) else ""}
            <div class="choice"><button type="button" class="yes on" data-v="approved">✅ Погоджую</button>
              <button type="button" class="later" data-v="deferred">⏳ Пізніше</button></div></div>''' for x in rec["lines"])
        body = f'''<section><h2>Погодження кошторису</h2>
          <p class="muted">Позначте, що робимо зараз, а що — пізніше. Відкладене ми нагадаємо.</p>
          <form id="ap">{rows}
            <div class="total">Погоджено: <b id="apSum">{_m(rec["total"])} {cur}</b><span id="apUah"></span></div>
            <button type="submit" class="primary">Підтвердити вибір</button>
            <div id="apErr" class="err" hidden></div></form></section>'''
    else:
        decision = {x["key"]: x["decision"] for x in (rec or {}).get("lines", [])}
        chip = lambda key: {"approved": '<span class="chip ok">✅ погоджено</span>',  # noqa: E731
                            "deferred": '<span class="chip">⏳ пізніше</span>'}.get(decision.get(key), "")
        items = [(f"labor:{i}", x.name, x.qty, x.total) for i, x in enumerate(order.labor)] + \
                [(f"parts:{i}", x.name, x.qty, x.total) for i, x in enumerate(order.parts)]
        rows = "".join(f'''<div class="line"><div class="lt"><div><b>{e(name)}</b>{f' <span class="muted">× {e(qty)}</span>' if qty not in (1, 1.0) else ""} {chip(key)}</div>
            <div class="sum">{_m(total)} {cur}</div></div>
            {f'<div class="pics">{"".join(pic(p) for p in by_line.get(name, []))}</div>' if by_line.get(name) else ""}</div>'''
                       for key, name, qty, total in items) or '<p class="muted">—</p>'
        state = ""
        if rec and rec.get("status") in ("approved", "partial", "declined"):
            state = {"approved": '<div class="banner ok">✅ Кошторис погоджено</div>',
                     "partial": f'<div class="banner ok">✅ Погоджено на {_m(rec["approved_total"])} {cur}, решту відкладено</div>',
                     "declined": '<div class="banner">Ви відмовилися від робіт за цим кошторисом</div>'}[rec["status"]]
        body = f'<section><h2>Роботи й запчастини</h2>{state}{rows}</section>'

    t = order.totals()
    paid, balance = order.paid(), order.balance()
    money_block = f'''<section><h2>Вартість</h2>
      <div class="kv"><span>Разом</span><b>{_m(t["total"])} {cur}{in_uah(t["total"])}</b></div>
      {f'<div class="kv"><span>Сплачено</span><span>{_m(paid)} {cur}</span></div>' if paid else ""}
      {f'<div class="kv"><span>До сплати</span><b class="due">{_m(balance)} {cur}{in_uah(balance)}</b></div>' if paid and balance > 0 else ""}
      {'<div class="kv"><span></span><span class="okc">✓ Сплачено повністю</span></div>' if paid and balance <= 0 else ""}
      {f'<p class="muted small">{e(rate["source"])} {_m(rate["rate"])} грн/$ на {e(rate["date"])}</p>' if rate else ""}</section>'''

    gallery = [p for p in photos if p.get("to_client") and not p.get("line")]
    gallery_block = (f'<section><h2>Фото</h2><div class="pics big">{"".join(pic(p) for p in gallery)}</div></section>'
                     if gallery else "")
    recs = "".join(f"<li>{e(r)}</li>" for r in order.recommendations)
    docs = f'''<section><h2>Документи</h2><div class="docs">
      <a class="btn" href="{base}/pdf" target="_blank">📄 Замовлення-наряд (PDF)</a>
      {f'<a class="btn" href="{base}/act.pdf" target="_blank">📋 Акт приймання (PDF)</a>' if act else ""}</div></section>'''
    contacts = " · ".join(e(x) for x in (service.get("address"), service.get("phone")) if x)

    script = ""
    if pending:
        script = f'''<script>
const form = document.getElementById("ap"), RATE = {json.dumps(str(rate["rate"]) if rate else None)};
const fmt = v => v.toLocaleString("uk-UA", {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
function recount(){{
  let sum = 0;
  form.querySelectorAll(".line").forEach(l => {{ if (l.querySelector(".yes.on")) sum += Number(l.dataset.total); }});
  document.getElementById("apSum").textContent = fmt(sum) + " {cur}";
  document.getElementById("apUah").textContent = RATE ? " ≈ " + fmt(sum * Number(RATE)) + " грн" : "";
}}
form.addEventListener("click", ev => {{
  const b = ev.target.closest(".choice button"); if (!b) return;
  b.parentNode.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b)); recount();
}});
form.addEventListener("submit", async ev => {{
  ev.preventDefault();
  const decisions = {{}};
  form.querySelectorAll(".line").forEach(l => {{ decisions[l.dataset.key] = l.querySelector(".yes.on") ? "approved" : "deferred"; }});
  const btn = form.querySelector("button[type=submit]"); btn.disabled = true;
  try{{
    const r = await fetch("{base}/approve", {{method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify({{decisions}})}});
    const d = await r.json().catch(() => ({{}}));
    if (!r.ok) throw new Error(d.error || "Помилка " + r.status);
    location.reload();
  }}catch(err){{ const e = document.getElementById("apErr"); e.hidden = false; e.textContent = err.message; btn.disabled = false; }}
}});
recount();
</script>'''

    return f"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>№ {e(order.number)} · {e(service.get("name"))}</title>
<style>
:root{{--bg:#f4f5f7;--panel:#fff;--text:#16181d;--muted:#6b7280;--line:#e3e5e9;--accent:#d32f2f;--ok:#1b7f3b;--soft:#f0f1f4}}
@media (prefers-color-scheme: dark){{:root{{--bg:#0f1115;--panel:#171a20;--text:#e8eaed;--muted:#9aa1ad;--line:#2a2f38;--accent:#ef5350;--ok:#5fd38a;--soft:#20242c}}}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:16px/1.45 -apple-system,system-ui,"Segoe UI",Roboto,sans-serif}}
main{{max-width:640px;margin:0 auto;padding:16px 16px 40px}}
header{{margin-bottom:12px}} header .svc{{font-weight:700}} .muted{{color:var(--muted)}} .small{{font-size:13px}}
h1{{font-size:20px;margin:4px 0}} h2{{font-size:15px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:0 0 10px}}
section{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin:12px 0}}
.steps{{display:flex;list-style:none;padding:0;margin:12px 0;gap:4px}}
.steps li{{flex:1;text-align:center;font-size:13px;padding:8px 2px;border-radius:8px;background:var(--soft);color:var(--muted)}}
.steps li.done{{color:var(--ok)}} .steps li.now{{background:var(--accent);color:#fff;font-weight:600}}
.banner{{background:var(--soft);border-radius:10px;padding:10px 12px;margin:0 0 10px}} .banner.ok{{color:var(--ok)}}
.line{{border-top:1px solid var(--line);padding:10px 0}} .line:first-of-type{{border-top:0}}
.lt{{display:flex;justify-content:space-between;gap:10px}} .sum{{white-space:nowrap;font-weight:600}}
.choice{{display:flex;gap:8px;margin-top:8px}}
button,.btn{{font:inherit;border-radius:10px;border:1px solid var(--line);background:var(--panel);color:var(--text);padding:10px 14px;cursor:pointer;text-decoration:none;display:inline-block}}
.choice button{{flex:1}} .choice .yes.on{{background:var(--ok);border-color:var(--ok);color:#fff}} .choice .later.on{{background:var(--soft);border-color:var(--muted)}}
.primary{{background:var(--accent);border-color:var(--accent);color:#fff;width:100%;margin-top:10px;font-weight:600}}
.total{{margin-top:12px;font-size:17px}} .uah{{color:var(--muted);font-weight:400;font-size:14px}}
.kv{{display:flex;justify-content:space-between;padding:4px 0}} .due{{color:var(--accent)}} .okc{{color:var(--ok)}}
.chip{{font-size:12px;border-radius:999px;padding:2px 8px;background:var(--soft);color:var(--muted);white-space:nowrap}} .chip.ok{{color:var(--ok)}}
.pics{{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}} .pics img{{width:84px;height:64px;object-fit:cover;border-radius:8px;border:1px solid var(--line)}}
.pics.big img{{width:calc(50% - 3px);height:auto;aspect-ratio:4/3}} .pics.big a{{display:contents}}
.docs{{display:flex;flex-direction:column;gap:8px}} .err{{color:var(--accent);margin-top:8px}}
ul{{margin:0;padding-left:20px}} footer{{text-align:center;color:var(--muted);font-size:13px;margin-top:20px}}
</style></head><body><main>
<header><div class="svc">{e(service.get("name"))}</div><h1>Замовлення-наряд № {e(order.number)}</h1>
<div class="muted">{e(car) or "Tesla"}{" · від " + e(order.created_at.split(" ")[0]) if order.created_at else ""}</div></header>
{steps}
{body}
{money_block}
{gallery_block}
{f"<section><h2>Рекомендації</h2><ul>{recs}</ul></section>" if recs else ""}
{docs}
<footer>{e(service.get("name"))}{" · " + contacts if contacts else ""}</footer>
</main>{script}</body></html>"""
