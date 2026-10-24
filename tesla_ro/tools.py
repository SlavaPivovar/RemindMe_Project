"""Операции над заказ-нарядом, которые помощник вызывает как инструменты.

Каждый публичный метод OrderSession становится инструментом Claude: описание и
параметры берутся из docstring и сигнатуры.
"""

from __future__ import annotations

import json
from pathlib import Path

from anthropic.lib.tools import ToolError

from .catalog import Catalog
from .order import Customer, LaborLine, OrderStore, PartLine, RepairOrder, Vehicle
from .render import render_html
from .vin import decode_vin


def _json(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


class OrderSession:
    def __init__(self, catalog: Catalog, store: OrderStore):
        self.catalog = catalog
        self.store = store
        self.order: RepairOrder | None = None

    @property
    def _order(self) -> RepairOrder:
        if self.order is None:
            raise ToolError("Заказ-наряд ещё не создан. Сначала вызовите new_order.")
        return self.order

    def tool_methods(self) -> list:
        return [
            self.new_order, self.open_order, self.set_customer, self.set_vehicle, self.decode_vin,
            self.add_complaint, self.set_diagnosis, self.add_recommendation,
            self.search_operations, self.search_parts, self.add_operation, self.add_custom_labor,
            self.add_part, self.add_custom_part, self.set_discount, self.remove_line,
            self.get_order, self.save_order,
        ]

    # --- жизненный цикл ---

    def new_order(self) -> str:
        """Создать новый пустой заказ-наряд и сделать его текущим. Возвращает номер."""
        self.order = RepairOrder(
            number=self.store.next_number(),
            currency=self.catalog.currency,
            vat_percent=self.catalog.vat_percent,
            prices_include_vat=self.catalog.prices_include_vat,
        )
        return _json({"number": self.order.number})

    def open_order(self, number: str) -> str:
        """Открыть ранее сохранённый заказ-наряд для редактирования.

        Args:
            number: Номер заказ-наряда, например ЗН-2026-00001.
        """
        try:
            self.order = self.store.load(number)
        except FileNotFoundError:
            raise ToolError(f"Заказ-наряд {number} не найден") from None
        return self.get_order()

    # --- клиент и автомобиль ---

    def set_customer(self, name: str, phone: str = "", email: str = "") -> str:
        """Указать данные заказчика.

        Args:
            name: ФИО заказчика или название организации.
            phone: Контактный телефон.
            email: Адрес электронной почты.
        """
        self._order.customer = Customer(name=name.strip(), phone=phone.strip(), email=email.strip())
        return "ok"

    def decode_vin(self, vin: str) -> str:
        """Расшифровать VIN Tesla: модель, модельный год, завод, проверка контрольной цифры.

        Args:
            vin: 17-значный VIN.
        """
        info = decode_vin(vin)
        return _json(info.__dict__)

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
        """Указать автомобиль. Модель и год заполняются автоматически по VIN; передавайте model/year
        только если клиент их уточнил или VIN не расшифровывается.

        Args:
            vin: 17-значный VIN.
            odometer_km: Пробег в километрах.
            plate: Государственный регистрационный номер.
            color: Цвет кузова.
            battery_percent: Уровень заряда высоковольтной батареи при приёмке, %.
            model: Модель (Model S / 3 / X / Y / Cybertruck), если нужно задать вручную.
            year: Модельный год, если нужно задать вручную.
        """
        info = decode_vin(vin)
        if odometer_km is not None and odometer_km < 0:
            raise ToolError("Пробег не может быть отрицательным")
        if battery_percent is not None and not 0 <= battery_percent <= 100:
            raise ToolError("Заряд батареи должен быть от 0 до 100 %")
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
        return _json({"vehicle": self._order.vehicle.__dict__, "vin_warnings": info.warnings})

    # --- описание проблемы ---

    def add_complaint(self, text: str) -> str:
        """Добавить жалобу/причину обращения клиента (своими словами клиента, кратко).

        Args:
            text: Формулировка жалобы.
        """
        self._order.complaints.append(text.strip())
        return f"Жалоб: {len(self._order.complaints)}"

    def set_diagnosis(self, text: str) -> str:
        """Записать результаты диагностики / заключение мастера.

        Args:
            text: Текст заключения.
        """
        self._order.diagnosis = text.strip()
        return "ok"

    def add_recommendation(self, text: str) -> str:
        """Добавить рекомендацию клиенту (работы на будущее, не входящие в текущий заказ).

        Args:
            text: Текст рекомендации.
        """
        self._order.recommendations.append(text.strip())
        return "ok"

    # --- справочник ---

    def search_operations(self, query: str) -> str:
        """Найти работы в справочнике по ключевым словам (например «колодки», «кондиционер»).
        Учитывает модель автомобиля текущего заказа. Пустой запрос возвращает весь список.

        Args:
            query: Ключевые слова на русском.
        """
        model_code = self.order.vehicle.model_code if self.order else None
        ops = self.catalog.search_operations(query, model_code or None)
        return _json([
            {"code": o.code, "name": o.name, "hours": o.hours,
             "price": round(o.hours * self.catalog.labor_rate, 2), "parts": o.parts}
            for o in ops
        ])

    def search_parts(self, query: str) -> str:
        """Найти запчасти и материалы в справочнике по ключевым словам или артикулу.

        Args:
            query: Ключевые слова или артикул.
        """
        return _json([p.__dict__ for p in self.catalog.search_parts(query)])

    # --- строки заказа ---

    def add_operation(self, code: str, include_parts: bool = True, hours: float | None = None) -> str:
        """Добавить работу из справочника по коду. По умолчанию добавляются и типовые запчасти к ней.

        Args:
            code: Код работы из справочника (search_operations).
            include_parts: Добавить типовые запчасти/материалы для этой работы.
            hours: Нормо-часы, если нужно изменить норматив (например, для двух сторон).
        """
        op = self.catalog.operations.get(code)
        if op is None:
            raise ToolError(f"Работа с кодом {code} не найдена. Используйте search_operations.")
        order = self._order
        if not op.fits(order.vehicle.model_code or None):
            raise ToolError(f"Работа {code} не применима к {order.vehicle.model}")
        order.labor.append(LaborLine(code=op.code, name=op.name, hours=hours or op.hours, rate=self.catalog.labor_rate))
        added_parts = []
        if include_parts:
            for part_no in op.parts:
                self._add_part_line(part_no, 1)
                added_parts.append(part_no)
        return _json({"added_labor": op.code, "added_parts": added_parts, "totals": self._totals()})

    def add_custom_labor(self, name: str, hours: float, code: str = "CUSTOM") -> str:
        """Добавить работу, которой нет в справочнике (по ставке нормо-часа сервиса).

        Args:
            name: Наименование работы.
            hours: Нормо-часы.
            code: Код работы, если есть.
        """
        if hours <= 0:
            raise ToolError("Нормо-часы должны быть больше нуля")
        self._order.labor.append(LaborLine(code=code, name=name, hours=hours, rate=self.catalog.labor_rate))
        return _json({"totals": self._totals()})

    def add_part(self, part_no: str, qty: float = 1) -> str:
        """Добавить запчасть из справочника по артикулу.

        Args:
            part_no: Артикул из справочника (search_parts).
            qty: Количество.
        """
        self._add_part_line(part_no, qty)
        return _json({"totals": self._totals()})

    def add_custom_part(self, name: str, qty: float, price: float, part_no: str = "", unit: str = "шт") -> str:
        """Добавить запчасть, которой нет в справочнике (цену называет мастер).

        Args:
            name: Наименование.
            qty: Количество.
            price: Цена за единицу.
            part_no: Каталожный номер Tesla, если известен.
            unit: Единица измерения.
        """
        if qty <= 0 or price < 0:
            raise ToolError("Количество должно быть больше нуля, цена — не отрицательной")
        self._order.parts.append(PartLine(part_no=part_no, name=name, qty=qty, price=price, unit=unit))
        return _json({"totals": self._totals()})

    def _add_part_line(self, part_no: str, qty: float) -> None:
        part = self.catalog.parts.get(part_no)
        if part is None:
            raise ToolError(f"Запчасть {part_no} не найдена. Используйте search_parts или add_custom_part.")
        if qty <= 0:
            raise ToolError("Количество должно быть больше нуля")
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
        raise ToolError("kind должен быть 'labor' или 'parts'")

    def set_discount(self, kind: str, line_number: int, percent: float) -> str:
        """Установить скидку на строку заказа. line_number = 0 — на все строки этого вида.

        Args:
            kind: 'labor' — работы, 'parts' — запчасти.
            line_number: Номер строки (с 1), как в get_order; 0 — все строки.
            percent: Скидка в процентах (0–100).
        """
        if not 0 <= percent <= 100:
            raise ToolError("Скидка должна быть от 0 до 100 %")
        lines = self._lines(kind)
        targets = lines if line_number == 0 else [self._line(lines, line_number)]
        for line in targets:
            line.discount_percent = percent
        return _json({"totals": self._totals()})

    def remove_line(self, kind: str, line_number: int) -> str:
        """Удалить строку из заказа.

        Args:
            kind: 'labor' — работы, 'parts' — запчасти.
            line_number: Номер строки (с 1), как в get_order.
        """
        lines = self._lines(kind)
        removed = self._line(lines, line_number)
        lines.remove(removed)
        return _json({"removed": removed.name, "totals": self._totals()})

    @staticmethod
    def _line(lines: list, number: int):
        if not 1 <= number <= len(lines):
            raise ToolError(f"Нет строки №{number}; всего строк: {len(lines)}")
        return lines[number - 1]

    # --- просмотр и сохранение ---

    def _totals(self) -> dict:
        return {k: str(v) for k, v in self._order.totals().items()}

    def snapshot(self) -> dict | None:
        """Состояние заказа для интерфейса (не инструмент модели)."""
        if self.order is None:
            return None
        data = self.order.to_dict()
        data["missing_fields"] = self.order.missing_fields()
        return data

    def get_order(self) -> str:
        """Показать текущее состояние заказ-наряда: все поля, строки, итоги и чего не хватает."""
        data = self._order.to_dict()
        data["missing_fields"] = self._order.missing_fields()
        return _json(data)

    def save_order(self, status: str = "открыт") -> str:
        """Сохранить заказ-наряд (JSON + HTML для печати). Перед вызовом покажите клиенту итог и
        получите подтверждение.

        Args:
            status: Статус: 'черновик', 'открыт' (принят в работу), 'выполнен', 'закрыт'.
        """
        order = self._order
        missing = order.missing_fields()
        if missing and status != "черновик":
            raise ToolError(f"Нельзя сохранить со статусом «{status}»: не заполнено — {', '.join(missing)}. "
                            "Заполните поля или сохраните как 'черновик'.")
        order.status = status
        json_path = self.store.save(order)
        html_path = Path(json_path).with_suffix(".html")
        html_path.write_text(render_html(order, self.catalog.service), encoding="utf-8")
        return _json({"json": str(json_path), "html": str(html_path), "totals": self._totals()})
