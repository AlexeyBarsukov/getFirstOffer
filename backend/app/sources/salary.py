"""Зарплата, записанная текстом: «от 100 000 до 150 000 ₽» (RSS-ленты hh.ru и Хабра)."""

import re
from html import unescape
from typing import Any

CURRENCY_CODES = {"₽": "RUR", "руб.": "RUR", "$": "USD", "€": "EUR", "₸": "KZT", "Br": "BYR"}

_AMOUNT = r"(\d[\d  ]*)"


def parse_salary_text(text: str) -> dict[str, Any] | None:
    """«от 100 000 до 150 000 ₽» → {"from": 100000, "to": 150000, "currency": "RUR"}."""
    text = unescape(text).replace(" ", " ").strip()
    salary_from = re.search(rf"от\s*{_AMOUNT}", text, re.I)
    salary_to = re.search(rf"до\s*{_AMOUNT}", text, re.I)
    if not salary_from and not salary_to:
        return None
    currency = next((code for sign, code in CURRENCY_CODES.items() if text.endswith(sign)), None)
    return {
        "from": int(salary_from.group(1).replace(" ", "")) if salary_from else None,
        "to": int(salary_to.group(1).replace(" ", "")) if salary_to else None,
        "currency": currency,
        "gross": None,
    }
