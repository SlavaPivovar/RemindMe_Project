"""База клієнтів: збирається із замовлень-нарядів і даних Telegram-бота.

Окремого файлу клієнтів немає — джерело правди одне: замовлення-наряди. Клієнт визначається
за телефоном (останні 9 цифр: «+380 67 123 45 67» і «067 123 45 67» — той самий); замовлення без
телефону групуються за ПІБ. Клієнти, які підключились до бота, позначаються «в Telegram».
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .order import CANCELLED, DRAFT, RepairOrder


def phone_key(phone: str) -> str | None:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return digits[-9:] if len(digits) >= 9 else None


def client_key(order: RepairOrder) -> str | None:
    key = phone_key(order.customer.phone)
    if key:
        return key
    name = " ".join(order.customer.name.lower().split())
    return f"n:{name}" if name else None


def _date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%d.%m.%Y %H:%M")
    except ValueError:
        return datetime.min


def car_label(car: dict) -> str:
    return " ".join(x for x in (f"Tesla {car['model']}" if car.get("model") else "Tesla",
                                f"({car['plate']})" if car.get("plate") else "") if x)


def build_clients(orders: list[RepairOrder], telegram: dict | None = None) -> list[dict]:
    """Список клієнтів, від останнього візиту. telegram — links["clients"] бота: ключ -> {chat_id, name, at}."""
    telegram = telegram or {}
    clients: dict[str, dict] = {}
    for order in sorted(orders, key=lambda o: (_date(o.created_at), o.number)):  # від старих до нових: нові дані перекривають
        key = client_key(order)
        if key is None:
            continue
        c = clients.setdefault(key, {"key": key, "name": "", "phone": "", "email": "", "cars": {}, "orders": [],
                                     "visits": 0, "spent": Decimal("0"), "debt": Decimal("0"), "last_visit": "", "first_visit": ""})
        cust = order.customer
        c["name"] = cust.name or c["name"]
        c["phone"] = cust.phone or c["phone"]
        c["email"] = cust.email or c["email"]
        v = order.vehicle
        if v.vin or v.plate:
            car = c["cars"].setdefault(v.vin or f"plate:{v.plate}", {"vin": v.vin})
            car.update({k: val for k, val in (("model", v.model), ("year", v.year), ("plate", v.plate),
                                              ("odometer_km", v.odometer_km)) if val not in (None, "")})
            car["last_visit"] = order.created_at
        total = order.totals()["total"]
        c["orders"].append({"number": order.number, "date": order.created_at, "status": order.status,
                            "total": str(total), "currency": order.currency,
                            "vehicle": car_label({"model": v.model, "plate": v.plate}),
                            "works": [line.name for line in order.labor][:8]})
        if order.status not in (DRAFT, CANCELLED):
            c["visits"] += 1
            c["spent"] += total
            c["last_visit"] = order.created_at
            c["first_visit"] = c["first_visit"] or order.created_at
        if order.status == "видано" and order.balance() > 0:
            c["debt"] += order.balance()

    for key, tg in telegram.items():
        c = clients.get(key)
        if c is None:  # підключився до бота, але його замовлень уже немає (видалені)
            c = clients[key] = {"key": key, "name": tg.get("name", ""), "phone": "", "email": "", "cars": {},
                                "orders": [], "visits": 0, "spent": Decimal("0"), "debt": Decimal("0"), "last_visit": "", "first_visit": ""}
        c["telegram"] = {"name": tg.get("name", ""), "at": tg.get("at", ""), "blocked": bool(tg.get("blocked"))}

    result = []
    for c in clients.values():
        c.setdefault("telegram", None)
        c["cars"] = sorted(c["cars"].values(), key=lambda x: _date(x.get("last_visit", "")), reverse=True)
        c["orders"].reverse()  # нові згори
        c["spent"] = str(c["spent"].quantize(Decimal("0.01")))
        c["debt"] = str(c["debt"].quantize(Decimal("0.01")))
        c["last_order"] = c["orders"][0]["date"] if c["orders"] else ""
        result.append(c)
    result.sort(key=lambda c: _date(c["last_order"] or (c["telegram"] or {}).get("at", "")), reverse=True)
    return result


def months_since(value: str, now: datetime | None = None) -> float | None:
    d = _date(value)
    if d == datetime.min:
        return None
    return ((now or datetime.now()) - d).days / 30.44


def personalize(template: str, client: dict, service: dict) -> str:
    car = client["cars"][0] if client.get("cars") else {}
    return (template.replace("{ім'я}", client.get("name") or "")
            .replace("{авто}", car_label(car) if car else "ваш автомобіль")
            .replace("{сервіс}", service.get("name", ""))
            .replace("{адреса}", service.get("address", "")))
