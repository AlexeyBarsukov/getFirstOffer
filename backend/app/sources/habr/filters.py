"""Перевод фильтров подписки (в терминах hh.ru) в запросы к Хабр Карьере.

Хабр ищет по одному слову и одному городу, поэтому одна подписка — несколько запросов
(слово × город). Чего Хабр не умеет (опыт, «только в названии»), проверяем у себя.
"""

from dataclasses import dataclass

from app.sources.base import SearchFilters
from app.sources.http import QueryItems
from app.sources.keywords import Keywords, matches_title, parse_query
from app.sources.models import VacancyShort

# id регионов hh.ru → city_id Хабра (из ссылок вида /vacancies?city_id=698)
AREA_RUSSIA = "113"
CITIES = {
    "1": "678",  # Москва
    "2": "679",  # Санкт-Петербург
    "88": "698",  # Казань
}

# Опыт hh.ru → квалификации Хабра, которые ему соответствуют (с запасом в обе стороны)
QUALIFICATIONS = {
    "noExperience": {"intern", "junior"},
    "between1And3": {"junior", "middle"},
    "between3And6": {"middle", "senior"},
    "moreThan6": {"senior", "lead"},
}
WORK_FORMAT_REMOTE = "REMOTE"


@dataclass(frozen=True)
class HabrSearch:
    """Запросы к Хабру по одной подписке и проверка, подходит ли найденная вакансия."""

    queries: list[QueryItems]
    keywords: Keywords | None
    remote_only: bool
    qualifications: set[str]

    def matches(self, vacancy: VacancyShort) -> bool:
        keywords = self.keywords
        if keywords and keywords.title_only and not matches_title(vacancy.name, keywords):
            return False
        if self.remote_only and not any(f.id == WORK_FORMAT_REMOTE for f in vacancy.work_format):
            return False
        # Квалификацию указывают не всегда — вакансии без неё не отбрасываем
        if self.qualifications and vacancy.experience:
            return vacancy.experience.id in self.qualifications
        return True


def build_search(filters: SearchFilters) -> HabrSearch:
    keywords = parse_query(filters.text)
    remote_only = filters.work_format == [WORK_FORMAT_REMOTE]
    qualifications = set().union(*(QUALIFICATIONS.get(e, set()) for e in filters.experience))

    common: QueryItems = [("type", "all"), ("sort", "date")]
    if remote_only:
        common.append(("remote", "true"))
    if filters.salary is not None:
        common += [("salary", str(filters.salary)), ("currency", "RUR")]
    if filters.only_with_salary:
        common.append(("with_salary", "true"))

    if keywords is None:
        # Только роли без слов: соответствия ролям hh.ru на Хабре нет — не ищем
        return HabrSearch([], None, remote_only, qualifications)

    queries = [
        [*common, ("q", term), *([("city_id", city)] if city else [])]
        for term in keywords.terms
        for city in _cities(filters.areas)
    ]
    return HabrSearch(queries, keywords, remote_only, qualifications)


def _cities(areas: list[str]) -> list[str | None]:
    """city_id для запросов; [None] — без города. Регионы без пары на Хабре пропускаются."""
    if not areas or AREA_RUSSIA in areas:
        return [None]
    return [CITIES[a] for a in areas if a in CITIES]
