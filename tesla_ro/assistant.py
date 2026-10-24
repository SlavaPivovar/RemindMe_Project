"""Диалоговый помощник мастера-приёмщика на базе Claude."""

from __future__ import annotations

import anthropic
from anthropic import beta_tool

from .tools import OrderSession

MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """\
Ты — помощник мастера-приёмщика сервисного центра по ремонту автомобилей Tesla.
Твоя задача — вместе с мастером оформить заказ-наряд: собрать данные, подобрать работы и
запчасти из справочника и сохранить документ. Отвечай по-русски, коротко и по делу.

Порядок работы:
1. Если заказ-наряд не открыт — создай новый (new_order).
2. Собери данные заказчика (ФИО, телефон) и автомобиля (VIN, пробег; при желании гос. номер,
   цвет, заряд батареи). VIN всегда передавай в set_vehicle — модель и год определятся сами.
   Если расшифровка VIN выдала предупреждения, сообщи о них и попроси перепроверить.
3. Запиши жалобы клиента (add_complaint) — каждая отдельной строкой.
4. Подбери работы: ищи в справочнике (search_operations), предлагай мастеру подходящие позиции
   с нормо-часами и ценой. По неясным симптомам предлагай сначала диагностику. Не добавляй
   работы и запчасти без согласия мастера, если он явно не сказал «добавь».
   Если подходящей работы или детали нет в справочнике — используй add_custom_labor /
   add_custom_part, но цену и нормо-часы бери только от мастера, не выдумывай.
5. Перед сохранением покажи сводку (get_order): работы, запчасти, итог и незаполненные поля.
   После подтверждения сохрани (save_order) и назови пути к файлам.

Мастер может давать данные в любом порядке и сразу пачкой — разбирай всё, что есть в сообщении,
и спрашивай только недостающее. Суммы называй с валютой. Помни о безопасности: работы с
высоковольтной системой указывай отдельно, при повреждении ВВБ рекомендуй диагностику.
"""


class RepairOrderAssistant:
    def __init__(
        self,
        session: OrderSession,
        client: anthropic.Anthropic | None = None,
        model: str = MODEL,
        effort: str = "medium",
    ):
        self.session = session
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.tools = [beta_tool(m) for m in session.tool_methods()]
        self.messages: list = []

    def send(self, text: str) -> str:
        """Отправить реплику мастера и вернуть ответ помощника (после всех вызовов инструментов).

        При ошибке API история диалога откатывается к состоянию до этой реплики.
        """
        start = len(self.messages)
        self.messages.append({"role": "user", "content": text})
        try:
            last = self._run()
        except Exception:
            del self.messages[start:]
            raise

        if last is None:
            return ""
        if last.stop_reason == "refusal":
            return "Модель отказалась отвечать на этот запрос. Переформулируйте, пожалуйста."
        reply = "\n".join(b.text for b in last.content if b.type == "text").strip()
        if last.stop_reason == "max_tokens":
            reply += "\n[ответ обрезан по лимиту длины]"
        return reply

    def _run(self):
        runner = self.client.beta.messages.tool_runner(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=self.tools,
            messages=list(self.messages),
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
            # При отказе классификатора запрос повторяется на рекомендованной модели.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        last = None
        for message in runner:
            last = message
            # Раннер хранит свою копию истории — зеркалируем её, чтобы продолжить диалог.
            self.messages.append({"role": "assistant", "content": message.content})
            tool_response = runner.generate_tool_call_response()
            if tool_response is not None:
                self.messages.append(tool_response)
        return last
