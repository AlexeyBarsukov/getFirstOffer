"""Перевод фильтров подписки (в терминах hh.ru) в параметры API SuperJob.

https://api.superjob.ru/#search_vacancies. Ключевые слова — одним запросом («любое из
слов», в названии или во всём тексте). Чего SuperJob не умеет точно, проверяем у себя:
слова в названии, опыт, удалёнку (её почти никогда не отмечают полем place_of_work —
пишут в тексте, см. parsing.is_remote).
"""

from dataclasses import dataclass

from app.sources.base import SearchFilters
from app.sources.http import QueryItems
from app.sources.keywords import Keywords, matches_title, parse_query
from app.sources.models import VacancyShort

AREA_RUSSIA = "113"
COUNTRY_RUSSIA = "1"
# id регионов hh.ru → id городов SuperJob (GET /2.0/towns/?keyword=...)
TOWNS = {
    "1": "4",  # Москва
    "2": "14",  # Санкт-Петербург
    "88": "55",  # Казань
}
# Профессиональные роли hh.ru → разделы каталога SuperJob (GET /2.0/catalogues/, раздел 33 «IT»)
CATALOGUES = {
    "96": "48",  # Программист, разработчик → Разработка, программирование
    "121": "57",  # Специалист техподдержки → Техническая поддержка
    "124": "56",  # Тестировщик → Тестирование, QA
    "160": "628",  # DevOps
    "113": "51",  # Сисадмин → Системное администрирование
    "112": "49",  # Сетевой инженер → Сетевые технологии
    "116": "546",  # Инфобез → Информационная безопасность
    "10": "38",  # Аналитик → Аналитика
    "148": "38",
    "156": "38",
    "165": "627",  # Data Scientist → Data Science
    "126": "614",  # Техписатель → Техническая документация
}
WORK_FORMAT_REMOTE = "REMOTE"
# Где искать слова: 1 — в названии, 10 — во всём тексте вакансии
SEARCH_IN_TITLE = "1"
SEARCH_IN_TEXT = "10"
# Не больше 100 на страницу и 500 на запрос
PER_PAGE = 100


@dataclass(frozen=True)
class SuperJobSearch:
    params: QueryItems | None
    keywords: Keywords | None
    remote_only: bool
    experience: set[str]

    def matches(self, vacancy: VacancyShort) -> bool:
        keywords = self.keywords
        if keywords and keywords.title_only and not matches_title(vacancy.name, keywords):
            return False
        if self.remote_only and not any(f.id == WORK_FORMAT_REMOTE for f in vacancy.work_format):
            return False
        if self.experience and vacancy.experience:
            return vacancy.experience.id in self.experience
        return True


def build_search(filters: SearchFilters) -> SuperJobSearch:
    """params=None — подписку на SuperJob искать не по чему (регион без пары, нет слов и ролей)."""
    keywords = parse_query(filters.text)
    remote_only = filters.work_format == [WORK_FORMAT_REMOTE]
    experience = set(filters.experience)
    search = SuperJobSearch(None, keywords, remote_only, experience)

    params: QueryItems = [("order_field", "date"), ("order_direction", "desc")]
    if keywords:
        params += [
            ("keywords[0][srws]", SEARCH_IN_TITLE if keywords.title_only else SEARCH_IN_TEXT),
            ("keywords[0][skwc]", "or"),
            ("keywords[0][keys]", " ".join(keywords.terms)),
        ]
    else:
        catalogues = sorted({CATALOGUES[r] for r in filters.professional_roles if r in CATALOGUES})
        if not catalogues:
            return search
        params.append(("catalogues", ",".join(catalogues)))

    if not filters.areas or AREA_RUSSIA in filters.areas:
        params.append(("c", COUNTRY_RUSSIA))
    else:
        towns = [TOWNS[a] for a in filters.areas if a in TOWNS]
        if not towns:
            return search
        # Несколько городов — t[]; town принимает только один
        params += [("t[]", t) for t in towns]

    if filters.salary is not None:
        params.append(("payment_from", str(filters.salary)))
    if filters.only_with_salary:
        params.append(("no_agreement", "1"))
    return SuperJobSearch(params, keywords, remote_only, experience)
