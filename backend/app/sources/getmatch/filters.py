"""Фильтры подписки для getmatch. Сервер фильтры не применяет — всё проверяем у себя.

Вакансия подходит, если в ней есть требование к месту работы (location_requirements),
у которого подходят и формат, и место.
"""

from dataclasses import dataclass
from typing import Any

from app.sources.base import SearchFilters
from app.sources.getmatch.parsing import requirements
from app.sources.keywords import Keywords, matches_title, parse_query

AREA_RUSSIA = "113"
# id регионов hh.ru → location_id getmatch (начало: «kazan__tatarstan__russia»)
CITIES = {
    "1": "moscow",
    "2": "saint-petersburg",
    "88": "kazan",
}
# Регионы, в которые входит Россия: удалёнка «Весь мир» подходит и из России
RUSSIA_PARENTS = frozenset({"russia", "_cu-world", "_cu-emea", "_cu-europe", "_cu-cis"})
# Форматы hh.ru → форматы getmatch
FORMATS = {"REMOTE": "remote", "HYBRID": "hybrid", "ON_SITE": "office"}


@dataclass(frozen=True)
class GetmatchSearch:
    keywords: Keywords | None
    formats: set[str]
    # None — вся Россия
    cities: set[str] | None
    salary: int | None
    only_with_salary: bool

    def matches(self, raw: dict[str, Any]) -> bool:
        if self.keywords is None or not self._keywords_match(raw):
            return False
        if self.only_with_salary and raw.get("salary_hidden"):
            return False
        if self.salary is not None and not raw.get("salary_hidden"):
            top = raw.get("salary_display_to") or raw.get("salary_display_from")
            if top is not None and top < self.salary:
                return False
        return any(self._place_matches(r) for r in requirements(raw))

    def _keywords_match(self, raw: dict[str, Any]) -> bool:
        assert self.keywords is not None
        if matches_title(raw.get("position") or "", self.keywords):
            return True
        if self.keywords.title_only:
            return False
        text = f"{raw.get('offer_description') or ''} {raw.get('description_html') or ''}"
        return matches_title(text, self.keywords)

    def _place_matches(self, req: dict[str, Any]) -> bool:
        if self.formats and req.get("format") not in self.formats:
            return False
        places = {req.get("location_id") or "", *(req.get("ancestors") or [])}
        if self.cities is None:
            return bool(places & RUSSIA_PARENTS)
        return any(p.split("__")[0] in self.cities for p in places)


def build_search(filters: SearchFilters) -> GetmatchSearch:
    """Без ключевых слов не ищем: соответствия ролям hh.ru на getmatch нет."""
    if not filters.areas or AREA_RUSSIA in filters.areas:
        cities: set[str] | None = None
    else:
        cities = {CITIES[a] for a in filters.areas if a in CITIES}
    return GetmatchSearch(
        keywords=parse_query(filters.text),
        formats={FORMATS[f] for f in filters.work_format if f in FORMATS},
        cities=cities,
        salary=filters.salary,
        only_with_salary=filters.only_with_salary,
    )
