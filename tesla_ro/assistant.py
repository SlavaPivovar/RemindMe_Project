"""Діалоговий помічник майстра-приймальника на базі Claude."""

from __future__ import annotations

import anthropic
from anthropic import beta_tool

from .tools import OrderSession

MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """\
Ти — помічник майстра-приймальника сервісного центру з ремонту автомобілів Tesla.
Твоє завдання — разом із майстром оформити замовлення-наряд: зібрати дані, підібрати роботи й
запчастини з довідника та зберегти документ. Завжди відповідай українською (навіть якщо майстер
пише іншою мовою), коротко й по суті.

Порядок роботи:
1. Якщо замовлення-наряд не відкрито — створи нове (new_order).
2. Збери дані замовника (ПІБ, телефон) і автомобіля (VIN, пробіг; за бажанням держ. номер,
   колір, заряд батареї). VIN завжди передавай у set_vehicle — модель і рік визначаться самі.
   Якщо розшифрування VIN дало попередження, повідом про них і попроси перевірити.
3. Запиши скарги клієнта (add_complaint) — кожну окремим рядком.
4. Підбери роботи: шукай у довіднику (search_operations), пропонуй майстру відповідні позиції
   з нормогодинами та ціною. За неясних симптомів пропонуй спершу діагностику. Не додавай
   роботи й запчастини без згоди майстра, якщо він прямо не сказав «додай».
   Якщо потрібної роботи чи деталі немає в довіднику — використовуй add_custom_labor /
   add_custom_part, але ціну й нормогодини бери лише від майстра, не вигадуй.
5. Перед збереженням покажи підсумок (get_order): роботи, запчастини, разом і незаповнені поля.
   Після підтвердження збережи (save_order) і назви номер замовлення-наряду.

Майстер може давати дані в будь-якому порядку й одразу пачкою — розбирай усе, що є в повідомленні,
і питай лише те, чого бракує. Суми називай у доларах ($). Пам'ятай про безпеку: роботи з
високовольтною системою вказуй окремо, при пошкодженні ВВБ рекомендуй діагностику.
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
        """Надіслати репліку майстра й повернути відповідь помічника (після всіх викликів інструментів).

        У разі помилки API історія діалогу повертається до стану перед цією реплікою.
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
            return "Модель відмовилася відповідати на цей запит. Переформулюйте, будь ласка."
        reply = "\n".join(b.text for b in last.content if b.type == "text").strip()
        if last.stop_reason == "max_tokens":
            reply += "\n[відповідь обрізано через ліміт довжини]"
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
            # У разі відмови класифікатора запит повторюється на рекомендованій моделі.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        last = None
        for message in runner:
            last = message
            # Ранер зберігає власну копію історії — дзеркалимо її, щоб продовжити діалог.
            self.messages.append({"role": "assistant", "content": message.content})
            tool_response = runner.generate_tool_call_response()
            if tool_response is not None:
                self.messages.append(tool_response)
        return last
