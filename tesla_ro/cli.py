"""Консольный интерфейс помощника: python -m tesla_ro"""

from __future__ import annotations

import argparse
import sys

import anthropic

from .assistant import MODEL, RepairOrderAssistant
from .catalog import DEFAULT_CATALOG, Catalog
from .order import OrderStore
from .tools import OrderSession

BANNER = """\
Помощник по оформлению заказ-нарядов Tesla.
Опишите клиента, автомобиль и проблему — помощник подберёт работы и запчасти.
Команды: /заказ — показать текущий заказ, /выход — завершить.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Помощник по заказ-нарядам Tesla")
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG), help="JSON-справочник работ и запчастей")
    parser.add_argument("--orders-dir", default="orders", help="каталог для сохранения заказ-нарядов")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    args = parser.parse_args(argv)

    session = OrderSession(Catalog.load(args.catalog), OrderStore(args.orders_dir))
    assistant = RepairOrderAssistant(session, model=args.model, effort=args.effort)

    print(BANNER)
    while True:
        try:
            text = input("Мастер> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text in ("/выход", "/exit", "/quit"):
            return 0
        if text in ("/заказ", "/order"):
            print(session.get_order() if session.order else "Заказ-наряд ещё не создан.")
            continue
        try:
            print(f"\nПомощник> {assistant.send(text)}\n")
        except anthropic.AuthenticationError:
            print("Ошибка авторизации: задайте переменную окружения ANTHROPIC_API_KEY.", file=sys.stderr)
            return 1
        except anthropic.RateLimitError:
            print("Превышен лимит запросов, попробуйте через минуту.", file=sys.stderr)
        except anthropic.APIStatusError as exc:
            print(f"Ошибка API ({exc.status_code}): {exc.message}", file=sys.stderr)
        except anthropic.APIConnectionError:
            print("Нет соединения с API, проверьте сеть.", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
