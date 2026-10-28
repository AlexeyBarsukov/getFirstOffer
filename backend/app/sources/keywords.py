"""Ключевые слова подписки для площадок, которые не понимают язык запросов hh.ru.

Подписки хранят запрос в синтаксисе hh.ru: пресеты и бот (parse_keywords) пишут
`NAME:(frontend OR "front-end" OR react)`. Другие площадки ищут по одному слову, поэтому
запрос разбирается на слова, а «только в названии» проверяется у нас (matches_title).
"""

import re
from dataclasses import dataclass

_NAME_GROUP = re.compile(r"^NAME:\((.*)\)$", re.S)
_OR = re.compile(r"\s+OR\s+")
_OTHER_OPERATORS = re.compile(r"\b(AND|NOT)\b|[()]")


@dataclass(frozen=True)
class Keywords:
    terms: list[str]
    # Слово должно быть в названии вакансии (NAME: в запросе hh.ru)
    title_only: bool


def parse_query(text: str | None) -> Keywords | None:
    """Слова для поиска или None, если ключевых слов нет."""
    if not text or not text.strip():
        return None
    text = text.strip()
    title_only = False
    if match := _NAME_GROUP.match(text):
        text, title_only = match.group(1), True
    elif text.startswith("NAME:"):
        text, title_only = text.removeprefix("NAME:"), True
    if _OTHER_OPERATORS.search(text):
        # Сложный запрос (AND, NOT, скобки) не переводим — ищем его как фразу без операторов
        phrase = " ".join(_OTHER_OPERATORS.sub(" ", text).split())
        return Keywords([phrase], title_only) if phrase else None
    terms = [t.strip().strip('"').strip() for t in _OR.split(text)]
    return Keywords(_without_redundant([t for t in terms if t]), title_only)


def _without_redundant(terms: list[str]) -> list[str]:
    """Убирает слова, которые содержат другое слово из списка: поиск по «react» найдёт и
    «reactjs». Меньше запросов к площадке."""
    folded = [t.casefold() for t in terms]
    result: list[str] = []
    for term, low in zip(terms, folded, strict=True):
        if any(other != low and other in low for other in folded):
            continue
        if low not in (r.casefold() for r in result):
            result.append(term)
    return result


def matches_title(title: str, keywords: Keywords) -> bool:
    low = title.casefold()
    return any(term.casefold() in low for term in keywords.terms)
