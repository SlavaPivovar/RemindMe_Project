"""Консольний інтерфейс помічника: python -m tesla_ro"""

from __future__ import annotations

import argparse
import sys

import anthropic

from .assistant import MODEL, RepairOrderAssistant
from .catalog import DEFAULT_CATALOG, Catalog
from .order import OrderStore
from .tools import OrderSession

BANNER = """\
DK-TeslaService — помічник з оформлення замовлень-нарядів Tesla.
Опишіть клієнта, автомобіль і проблему — помічник підбере роботи та запчастини.
Команди: /замовлення — показати поточне замовлення, /вихід — завершити.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DK-TeslaService — помічник із замовлень-нарядів Tesla")
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG), help="JSON-довідник робіт і запчастин")
    parser.add_argument("--orders-dir", default="orders", help="каталог для збереження замовлень-нарядів")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    args = parser.parse_args(argv)

    session = OrderSession(Catalog.load(args.catalog), OrderStore(args.orders_dir))
    assistant = RepairOrderAssistant(session, model=args.model, effort=args.effort)

    print(BANNER)
    while True:
        try:
            text = input("Майстер> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text in ("/вихід", "/exit", "/quit"):
            return 0
        if text in ("/замовлення", "/order"):
            print(session.get_order() if session.order else "Замовлення-наряд ще не створено.")
            continue
        try:
            print(f"\nПомічник> {assistant.send(text)}\n")
        except anthropic.AuthenticationError:
            print("Помилка авторизації: задайте змінну оточення ANTHROPIC_API_KEY.", file=sys.stderr)
            return 1
        except anthropic.RateLimitError:
            print("Перевищено ліміт запитів, спробуйте за хвилину.", file=sys.stderr)
        except anthropic.APIStatusError as exc:
            print(f"Помилка API ({exc.status_code}): {exc.message}", file=sys.stderr)
        except anthropic.APIConnectionError:
            print("Немає з'єднання з API, перевірте мережу.", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
