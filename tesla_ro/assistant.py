"""Діалоговий помічник майстра-приймальника на базі Claude."""

from __future__ import annotations

import anthropic
from anthropic import beta_tool

from . import usage as usage_mod
from .tools import OrderSession

MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """\
Ти — помічник майстра-приймальника сервісного центру{service} з ремонту автомобілів Tesla.
Твоє завдання — разом із майстром оформити замовлення-наряд: зібрати дані, підібрати роботи й
запчастини з довідника та зберегти документ. Завжди відповідай українською (навіть якщо майстер
пише іншою мовою), коротко й по суті.

Порядок роботи:
1. Якщо замовлення-наряд не відкрито — створи нове (new_order).
2. Збери дані замовника (ПІБ, телефон) і автомобіля (VIN, пробіг; за бажанням держ. номер,
   колір, заряд батареї). VIN завжди передавай у set_vehicle — модель і рік визначаться самі.
   Якщо розшифрування VIN дало попередження, повідом про них і попроси перевірити.
   Якщо set_vehicle повернув попередні візити (previous_visits), коротко розкажи майстру: коли
   був останній візит і з яким пробігом, що робили, які рекомендації давали. Якщо рекомендації
   досі не виконані — запропонуй їх. Якщо дані замовника ще не вказані, запропонуй підставити
   клієнта з останнього візиту (після підтвердження — set_customer). Повну історію дає
   get_vehicle_history.
3. Запиши скарги клієнта (add_complaint) — кожну окремим рядком.
4. Підбери роботи: шукай у довіднику (search_operations), пропонуй майстру відповідні позиції
   з ціною. Ціни робіт фіксовані й беруться з довідника; змінюй ціну чи кількість рядка
   (set_line) лише на пряме прохання майстра. Якщо робота вказана на одну сторону, а потрібно
   дві — додай її з qty=2. За неясних симптомів пропонуй спершу діагностику. Не додавай
   роботи й запчастини без згоди майстра, якщо він прямо не сказав «додай».
   Якщо потрібної роботи чи деталі немає в довіднику — використовуй add_custom_labor /
   add_custom_part, але ціну бери лише від майстра, не вигадуй.
   Довідник поділено на розділи (діагностика, ТО, шини, гальма, підвіска, клімат, високовольтна
   система, привід, електрика, кузов). Якщо доречно, глянь get_maintenance_schedule і за пробігом
   та віком авто запропонуй планові роботи або запиши їх як рекомендації (add_recommendation).
5. Перед збереженням покажи підсумок (get_order): роботи, запчастини, разом і незаповнені поля.
   Після підтвердження збережи (save_order) і назви номер замовлення-наряду. Чернетка
   зберігається автоматично після кожної зміни, тож save_order потрібен, щоб прийняти
   замовлення в роботу (статус «прийнято») чи змінити статус. Статуси: чернетка → прийнято →
   в роботі → готово (роботи виконано) → видано (авто видано клієнту, оплачено); або скасовано.
   Майстер може міняти статус і сам у панелі замовлення-наряду.

Повідомлення можуть бути надиктовані голосом: числа можуть прийти словами, а VIN — літерами,
назвами літер чи з пробілами. Перетвори це на звичайний запис (VIN — 17 символів без пробілів)
і, якщо VIN надиктовано голосом або розшифрування дало попередження, повтори його майстру для
підтвердження.

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
        name = session.catalog.service.get("name", "")
        self.system = SYSTEM_PROMPT.replace("{service}", f" {name}" if name else "")
        self.tools = [beta_tool(m) for m in session.tool_methods()]
        self.messages: list = []
        self.last_turn_usage: dict = usage_mod.empty()  # витрати останньої репліки (усі запити ходу)

    def send(self, text: str) -> str:
        """Надіслати репліку майстра й повернути відповідь помічника (після всіх викликів інструментів).

        У разі помилки API історія діалогу повертається до стану перед цією реплікою.
        """
        start = len(self.messages)
        self.last_turn_usage = usage_mod.empty()
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
            system=self.system,
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
            # Рахуємо одразу: навіть якщо далі станеться помилка, за цей запит уже заплачено.
            usage_mod.add(self.last_turn_usage, usage_mod.from_response(message.usage, message.model))
            # Ранер зберігає власну копію історії — дзеркалимо її, щоб продовжити діалог.
            self.messages.append({"role": "assistant", "content": message.content})
            tool_response = runner.generate_tool_call_response()
            if tool_response is not None:
                self.messages.append(tool_response)
        return last
