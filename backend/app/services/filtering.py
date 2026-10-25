"""Локальные фильтры подписки, которых нет в API hh.ru."""

import re
from collections.abc import Iterable

from app.sources.hh_models import VacancyShort


def find_excluded_word(text: str, words: Iterable[str]) -> str | None:
    """Первое стоп-слово, встречающееся в тексте отдельным словом (без учёта регистра).

    «lead» исключает «Team Lead» и «lead-разработчик», но не «leading».
    """
    lowered = text.lower()
    for word in words:
        word = word.strip().lower()
        if word and re.search(rf"(?<!\w){re.escape(word)}(?!\w)", lowered):
            return word
    return None


def is_excluded(
    vacancy: VacancyShort, exclude_words: Iterable[str], exclude_employers: Iterable[str]
) -> bool:
    # Стоп-слова проверяем только в названии: в сниппете у junior-вакансий часто бывает
    # «под руководством senior-разработчика», и такие вакансии отсеивались бы зря
    if vacancy.employer.id is not None and vacancy.employer.id in set(exclude_employers):
        return True
    return find_excluded_word(vacancy.name, exclude_words) is not None
