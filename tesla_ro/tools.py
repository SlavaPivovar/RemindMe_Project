"""Операції над замовленням-нарядом, які помічник викликає як інструменти.

Кожен публічний метод OrderSession стає інструментом Claude: опис і
параметри беруться з docstring і сигнатури.
"""

from __future__ import annotations

import functools
import json
import logging
from pathlib import Path

from anthropic.lib.tools import ToolError

from .catalog import Catalog
from .order import DRAFT, Customer, LaborLine, OrderStore, PartLine, RepairOrder, StatusError, Vehicle
from .render import render_html
from .vin import decode_vin

log = logging.getLogger(__name__)


def autosave(method):
    """Після успішної зміни замовлення зберегти його на диск (чернетку не втратимо при перезапуску)."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        result = method(self, *args, **kwargs)
        self._autosave()
        return result
    return wrapper


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


class OrderSession:
    def __init__(self, catalog: Catalog, store: OrderStore):
        self.catalog = catalog
        self.store = store
        self.order: RepairOrder | None = None
        self.comments = None  # CommentStore: внутрішні коментарі для панелі (див. web.py)
        self.extras = None  # number -> dict: фото, погодження тощо для панелі (див. web.py)

    @property
    def _order(self) -> RepairOrder:
        if self.order is None:
            raise ToolError("Замовлення-наряд ще не створено. Спершу викличте new_order.")
        return self.order

    def tool_methods(self) -> list:
        return [
            self.new_order, self.open_order, self.set_customer, self.set_vehicle, self.decode_vin,
            self.get_vehicle_history,
            self.add_complaint, self.set_diagnosis, self.add_recommendation,
            self.search_operations, self.get_maintenance_schedule, self.search_parts, self.add_operation, self.add_custom_labor,
            self.add_part, self.add_custom_part, self.set_line, self.set_discount, self.remove_line,
            self.get_order, self.save_order,
        ]

    # --- життєвий цикл ---

    def new_order(self) -> str:
        """Створити нове порожнє замовлення-наряд і зробити його поточним. Повертає номер."""
        self.order = RepairOrder(
            number=self.store.next_number(),
            currency=self.catalog.currency,
            vat_percent=self.catalog.vat_percent,
            prices_include_vat=self.catalog.prices_include_vat,
        )
        return _json({"number": self.order.number})

    def open_order(self, number: str) -> str:
        """Відкрити раніше збережене замовлення-наряд для редагування.

        Args:
            number: Номер замовлення-наряду, наприклад ЗН-2026-00001.
        """
        try:
            self.order = self.store.load(number)
        except FileNotFoundError:
            raise ToolError(f"Замовлення-наряд {number} не знайдено") from None
        return self.get_order()

    # --- клієнт і автомобіль ---

    @autosave
    def set_customer(self, name: str, phone: str = "", email: str = "") -> str:
        """Вказати дані замовника.

        Args:
            name: ПІБ замовника або назва організації.
            phone: Контактний телефон.
            email: Адреса електронної пошти.
        """
        self._order.customer = Customer(name=name.strip(), phone=phone.strip(), email=email.strip())
        return "ok"

    def decode_vin(self, vin: str) -> str:
        """Розшифрувати VIN Tesla: модель, модельний рік, завод, перевірка контрольної цифри.

        Args:
            vin: 17-значний VIN.
        """
        info = decode_vin(vin)
        return _json(info.__dict__)

    @autosave
    def set_vehicle(
        self,
        vin: str,
        odometer_km: int | None = None,
        plate: str = "",
        color: str = "",
        battery_percent: int | None = None,
        model: str = "",
        year: int | None = None,
    ) -> str:
        """Вказати автомобіль. Модель і рік заповнюються автоматично за VIN; передавайте model/year
        лише якщо клієнт їх уточнив або VIN не розшифровується.

        Args:
            vin: 17-значний VIN.
            odometer_km: Пробіг у кілометрах.
            plate: Державний реєстраційний номер.
            color: Колір кузова.
            battery_percent: Рівень заряду високовольтної батареї під час приймання, %.
            model: Модель (Model S / 3 / X / Y / Cybertruck), якщо треба задати вручну.
            year: Модельний рік, якщо треба задати вручну.
        """
        info = decode_vin(vin)
        if odometer_km is not None and odometer_km < 0:
            raise ToolError("Пробіг не може бути від'ємним")
        if battery_percent is not None and not 0 <= battery_percent <= 100:
            raise ToolError("Заряд батареї має бути від 0 до 100 %")
        self._order.vehicle = Vehicle(
            vin=info.vin,
            model=model or info.model_name or "",
            model_code=info.model_code or "",
            year=year or info.year,
            color=color,
            plate=plate.upper().replace(" ", ""),
            odometer_km=odometer_km,
            battery_percent=battery_percent,
        )
        previous = self.store.history(info.vin, exclude=self._order.number)
        return _json({"vehicle": self._order.vehicle.__dict__, "vin_warnings": info.warnings,
                      "previous_visits_count": len(previous),
                      "previous_visits": [self._visit_info(o) for o in previous[:5]]})

    def get_vehicle_history(self, vin: str = "") -> str:
        """Історія автомобіля: попередні замовлення-наряди за VIN (дати, пробіг, скарги, роботи,
        рекомендації, дані клієнта), від найновішого.

        Args:
            vin: VIN; якщо не вказано — береться VIN поточного замовлення.
        """
        vin = vin or (self.order.vehicle.vin if self.order else "")
        if not vin:
            raise ToolError("Вкажіть VIN або спершу задайте автомобіль у замовленні")
        exclude = self.order.number if self.order else None
        return _json([self._visit_info(o) for o in self.store.history(vin, exclude=exclude)[:20]])

    @staticmethod
    def _visit_info(o: RepairOrder) -> dict:
        return {
            "number": o.number, "date": o.created_at, "status": o.status,
            "odometer_km": o.vehicle.odometer_km,
            "customer": {"name": o.customer.name, "phone": o.customer.phone, "email": o.customer.email},
            "complaints": o.complaints, "diagnosis": o.diagnosis,
            "works": [line.name for line in o.labor], "parts": [line.name for line in o.parts],
            "recommendations": o.recommendations,
            "total": str(o.totals()["total"]), "currency": o.currency,
        }

    # --- опис проблеми ---

    @autosave
    def add_complaint(self, text: str) -> str:
        """Додати скаргу / причину звернення клієнта (словами клієнта, коротко).

        Args:
            text: Формулювання скарги.
        """
        self._order.complaints.append(text.strip())
        return f"Скарг: {len(self._order.complaints)}"

    @autosave
    def set_diagnosis(self, text: str) -> str:
        """Записати результати діагностики / висновок майстра.

        Args:
            text: Текст висновку.
        """
        self._order.diagnosis = text.strip()
        return "ok"

    @autosave
    def add_recommendation(self, text: str) -> str:
        """Додати рекомендацію клієнту (роботи на майбутнє, що не входять у поточне замовлення).

        Args:
            text: Текст рекомендації.
        """
        self._order.recommendations.append(text.strip())
        return "ok"

    @autosave
    def remove_complaint(self, number: int) -> str:
        """Видалити скаргу за номером (з 1).

        Args:
            number: Номер скарги в списку.
        """
        self._line(self._order.complaints, number)  # перевірка номера
        self._order.complaints.pop(number - 1)
        return "ok"

    @autosave
    def remove_recommendation(self, number: int) -> str:
        """Видалити рекомендацію за номером (з 1).

        Args:
            number: Номер рекомендації в списку.
        """
        self._line(self._order.recommendations, number)  # перевірка номера
        self._order.recommendations.pop(number - 1)
        return "ok"

    # --- довідник ---

    def search_operations(self, query: str) -> str:
        """Знайти роботи в довіднику за ключовими словами (наприклад «колодки», «кондиціонер»).
        Враховує модель автомобіля поточного замовлення. Порожній запит повертає весь список.

        Args:
            query: Ключові слова українською.
        """
        model_code = self.order.vehicle.model_code if self.order else None
        ops = self.catalog.search_operations(query, model_code or None)
        return _json([self._op_info(o) for o in ops])

    def get_maintenance_schedule(self) -> str:
        """Показати регламентні роботи з рекомендованими інтервалами для моделі поточного автомобіля.
        Використовуй, щоб порадити клієнту планове обслуговування за пробігом і віком авто."""
        model_code = self.order.vehicle.model_code if self.order else None
        return _json([self._op_info(o) for o in self.catalog.operations.values()
                      if o.interval and o.fits(model_code or None)])

    def _op_info(self, o) -> dict:
        parts_price = sum(self.catalog.parts[p].price for p in o.parts if p in self.catalog.parts)
        return {"code": o.code, "name": o.name, "category": o.category, "price": o.price, "parts": o.parts,
                "parts_price": round(parts_price, 2), "interval": o.interval}

    def search_parts(self, query: str) -> str:
        """Знайти запчастини та матеріали в довіднику за ключовими словами або артикулом.

        Args:
            query: Ключові слова або артикул.
        """
        return _json([p.__dict__ for p in self.catalog.search_parts(query)])

    # --- рядки замовлення ---

    @autosave
    def add_operation(
        self, code: str, include_parts: bool = True, qty: float = 1, price: float | None = None
    ) -> str:
        """Додати роботу з довідника за кодом за фіксованою ціною з довідника.
        За замовчуванням додаються й типові запчастини до неї (у тій самій кількості).

        Args:
            code: Код роботи з довідника (search_operations).
            include_parts: Додати типові запчастини/матеріали для цієї роботи.
            qty: Кількість (наприклад, 2 — для обох сторін, якщо робота вказана на одну сторону).
            price: Інша ціна за одиницю — лише якщо майстер прямо назвав іншу ціну для цього замовлення.
        """
        op = self.catalog.operations.get(code)
        if op is None:
            raise ToolError(f"Роботу з кодом {code} не знайдено. Скористайтеся search_operations.")
        order = self._order
        if not op.fits(order.vehicle.model_code or None):
            raise ToolError(f"Робота {code} не застосовна до {order.vehicle.model}")
        if qty <= 0:
            raise ToolError("Кількість має бути більша за нуль")
        if price is not None and price < 0:
            raise ToolError("Ціна не може бути від'ємною")
        order.labor.append(LaborLine(code=op.code, name=op.name, price=op.price if price is None else price, qty=qty))
        added_parts = []
        if include_parts:
            for part_no in op.parts:
                self._add_part_line(part_no, qty)
                added_parts.append(part_no)
        return _json({"added_labor": op.code, "added_parts": added_parts, "totals": self._totals()})

    @autosave
    def add_custom_labor(self, name: str, price: float, qty: float = 1, code: str = "CUSTOM") -> str:
        """Додати роботу, якої немає в довіднику (ціну називає майстер).

        Args:
            name: Назва роботи.
            price: Ціна за одиницю.
            qty: Кількість.
            code: Код роботи, якщо є.
        """
        if price < 0 or qty <= 0:
            raise ToolError("Ціна має бути невід'ємною, кількість — більшою за нуль")
        self._order.labor.append(LaborLine(code=code, name=name, price=price, qty=qty))
        return _json({"totals": self._totals()})

    @autosave
    def add_part(self, part_no: str, qty: float = 1) -> str:
        """Додати запчастину з довідника за артикулом.

        Args:
            part_no: Артикул із довідника (search_parts).
            qty: Кількість.
        """
        self._add_part_line(part_no, qty)
        return _json({"totals": self._totals()})

    @autosave
    def add_custom_part(self, name: str, qty: float, price: float, part_no: str = "", unit: str = "шт") -> str:
        """Додати запчастину, якої немає в довіднику (ціну називає майстер).

        Args:
            name: Назва.
            qty: Кількість.
            price: Ціна за одиницю.
            part_no: Каталожний номер Tesla, якщо відомий.
            unit: Одиниця виміру.
        """
        if qty <= 0 or price < 0:
            raise ToolError("Кількість має бути більша за нуль, ціна — невід'ємна")
        self._order.parts.append(PartLine(part_no=part_no, name=name, qty=qty, price=price, unit=unit))
        return _json({"totals": self._totals()})

    def _add_part_line(self, part_no: str, qty: float) -> None:
        part = self.catalog.parts.get(part_no)
        if part is None:
            raise ToolError(f"Запчастину {part_no} не знайдено. Скористайтеся search_parts або add_custom_part.")
        if qty <= 0:
            raise ToolError("Кількість має бути більша за нуль")
        for line in self._order.parts:
            if line.part_no == part_no:
                line.qty += qty
                return
        self._order.parts.append(PartLine(part_no=part.part_no, name=part.name, qty=qty, price=part.price, unit=part.unit))

    def _lines(self, kind: str) -> list:
        if kind == "labor":
            return self._order.labor
        if kind == "parts":
            return self._order.parts
        raise ToolError("kind має бути 'labor' або 'parts'")

    @autosave
    def set_discount(self, kind: str, line_number: int, percent: float) -> str:
        """Встановити знижку на рядок замовлення. line_number = 0 — на всі рядки цього виду.

        Args:
            kind: 'labor' — роботи, 'parts' — запчастини.
            line_number: Номер рядка (з 1), як у get_order; 0 — усі рядки.
            percent: Знижка у відсотках (0–100).
        """
        if not 0 <= percent <= 100:
            raise ToolError("Знижка має бути від 0 до 100 %")
        lines = self._lines(kind)
        targets = lines if line_number == 0 else [self._line(lines, line_number)]
        for line in targets:
            line.discount_percent = percent
        return _json({"totals": self._totals()})

    @autosave
    def set_line(self, kind: str, line_number: int, price: float | None = None, qty: float | None = None) -> str:
        """Змінити ціну за одиницю та/або кількість рядка в поточному замовленні (довідник не змінюється).
        Використовуй, лише коли майстер прямо просить іншу ціну чи кількість.

        Args:
            kind: 'labor' — роботи, 'parts' — запчастини.
            line_number: Номер рядка (з 1), як у get_order.
            price: Нова ціна за одиницю.
            qty: Нова кількість.
        """
        if price is None and qty is None:
            raise ToolError("Вкажіть нову ціну або кількість")
        if (price is not None and price < 0) or (qty is not None and qty <= 0):
            raise ToolError("Ціна має бути невід'ємною, кількість — більшою за нуль")
        line = self._line(self._lines(kind), line_number)
        if price is not None:
            line.price = price
        if qty is not None:
            line.qty = qty
        return _json({"line": line.name, "price": line.price, "qty": line.qty, "totals": self._totals()})

    @autosave
    def remove_line(self, kind: str, line_number: int) -> str:
        """Видалити рядок із замовлення.

        Args:
            kind: 'labor' — роботи, 'parts' — запчастини.
            line_number: Номер рядка (з 1), як у get_order.
        """
        lines = self._lines(kind)
        removed = self._line(lines, line_number)
        lines.remove(removed)
        return _json({"removed": removed.name, "totals": self._totals()})

    @staticmethod
    def _line(lines: list, number: int):
        if not 1 <= number <= len(lines):
            raise ToolError(f"Немає рядка №{number}; усього рядків: {len(lines)}")
        return lines[number - 1]

    # --- перегляд і збереження ---

    def _totals(self) -> dict:
        return {k: str(v) for k, v in self._order.totals().items()}

    def _autosave(self) -> None:
        order = self.order
        if order is None:
            return
        has_content = any((order.customer.name, order.customer.phone, order.vehicle.vin, order.complaints,
                           order.diagnosis, order.recommendations, order.labor, order.parts))
        if not has_content:
            return  # порожні чернетки не засмічують список збережених
        try:
            self.store.save(order)
        except OSError:
            log.exception("Не вдалося автоматично зберегти %s", order.number)

    def snapshot(self) -> dict | None:
        """Стан замовлення для інтерфейсу (не інструмент моделі)."""
        if self.order is None:
            return None
        data = self.order.to_dict()
        data["missing_fields"] = self.order.missing_fields()
        data["history"] = [self._visit_info(o) for o in self.store.history(self.order.vehicle.vin,
                                                                           exclude=self.order.number)[:10]]
        data["comments"] = self.comments.list(self.order.number) if self.comments else []
        if self.extras:
            data.update(self.extras(self.order.number))
        return data

    def get_order(self) -> str:
        """Показати поточний стан замовлення-наряду: усі поля, рядки, підсумки та чого бракує."""
        data = self._order.to_dict()
        data["missing_fields"] = self._order.missing_fields()
        return _json(data)

    def save_order(self, status: str = "прийнято") -> str:
        """Зберегти замовлення-наряд зі статусом (JSON + HTML для друку). Перед прийняттям у роботу покажіть
        підсумок і отримайте підтвердження майстра.

        Args:
            status: 'чернетка'; 'прийнято' — прийнято в роботу; 'в роботі'; 'готово' — роботи виконано,
                авто чекає на клієнта; 'видано' — авто видано, оплачено; 'скасовано'.
        """
        order = self._order
        try:
            order.set_status(status)
        except StatusError as exc:
            raise ToolError(f"{exc}. Заповніть поля або залиште як '{DRAFT}'.") from None
        json_path = self.store.save(order)
        html_path = Path(json_path).with_suffix(".html")
        html_path.write_text(render_html(order, self.catalog.service), encoding="utf-8")
        return _json({"json": str(json_path), "html": str(html_path), "totals": self._totals()})
