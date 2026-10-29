"""«Работа России»: открытый API Роструда (https://opendata.trudvsem.ru), без ключа.

Особенности:
- поиск по тексту очень неточный (по «frontend» приходят тестировщики) — слово
  проверяем в названии, остальные фильтры тоже у нас;
- «или» API не понимает — по запросу на слово;
- offset — номер страницы, а не сдвиг; limit до 100;
- запрос идёт ~15 секунд, поэтому ответы кэшируются на 10 минут (подписки с одинаковыми
  словами делят их), а опрос — раз в 30 минут;
- одну вакансию отдаёт только по паре «ОГРН компании + uuid» — их берём из ссылки.
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from app.ratelimit import Clock, Sleep
from app.sources.base import SearchFilters
from app.sources.details import VacancyDetails
from app.sources.http import QueryItems, RetryingClient
from app.sources.keywords import Keywords, matches_title, parse_query
from app.sources.models import VacancyShort
from app.sources.portals import Portal
from app.sources.trudvsem.parsing import (
    card_ids,
    city_text,
    details_from_api,
    experience_code,
    is_quota_only,
    is_remote,
    vacancies,
    vacancy_from_api,
)

logger = structlog.get_logger(__name__)

BASE_URL = "https://opendata.trudvsem.ru/api/v1"
PER_PAGE = 100
MAX_PAGES = 10
CACHE_TTL = 10 * 60.0

AREA_RUSSIA = "113"
# id регионов hh.ru → код региона КЛАДР в API и слово для проверки адреса
REGIONS = {
    "1": ("7700000000000", "Москва"),
    "2": ("7800000000000", "Санкт-Петербург"),
    "88": ("1600000000000", "Казань"),
}


class TrudvsemClient:
    def __init__(
        self,
        user_agent: str,
        base_url: str = BASE_URL,
        min_request_interval: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        ttl: float = CACHE_TTL,
        clock: Clock = time.monotonic,
    ) -> None:
        self._http = RetryingClient(
            base_url,
            {"User-Agent": user_agent},
            min_request_interval,
            max_retries=2,
            timeout=60.0,
            transport=transport,
            sleep=sleep,
        )
        self._ttl = ttl
        self._clock = clock
        self._cache: dict[tuple[str, tuple[Any, ...]], tuple[float, list[dict[str, Any]]]] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def search(self, path: str, params: QueryItems) -> list[dict[str, Any]]:
        key = (path, tuple(params))
        cached = self._cache.get(key)
        if cached and self._clock() - cached[0] < self._ttl:
            return cached[1]
        found: list[dict[str, Any]] = []
        for page in range(MAX_PAGES):
            response = await self._http.get(
                path, [*params, ("offset", str(page)), ("limit", str(PER_PAGE))]
            )
            batch = vacancies(response.json())
            found += batch
            if len(batch) < PER_PAGE:
                break
        self._cache[key] = (self._clock(), found)
        return found

    async def vacancy(self, company_code: str, vacancy_uuid: str) -> dict[str, Any] | None:
        response = await self._http.get(f"/vacancies/vacancy/{company_code}/{vacancy_uuid}")
        found = vacancies(response.json())
        return found[0] if found else None


@dataclass(frozen=True)
class TrudvsemSearch:
    queries: list[tuple[str, QueryItems]]
    keywords: Keywords | None
    city: str | None
    remote_only: bool
    no_remote: bool
    experience: set[str]
    salary: int | None
    only_with_salary: bool

    def matches(self, raw: dict[str, Any], vacancy: VacancyShort) -> bool:
        if self.keywords is None or not matches_title(vacancy.name, self.keywords):
            # Поиск API слишком широкий — слово всегда проверяем в названии
            return False
        if is_quota_only(raw):
            return False
        remote = is_remote(raw)
        if (self.remote_only and not remote) or (self.no_remote and remote):
            return False
        if self.city and self.city.casefold() not in city_text(raw).casefold():
            return False
        if self.experience:
            years = (raw.get("requirement") or {}).get("experience")
            code = experience_code(int(years) if years is not None else None)
            if code and code not in self.experience:
                return False
        salary = vacancy.salary
        if self.only_with_salary and salary is None:
            return False
        if self.salary is not None and salary is not None:
            top = salary.to or salary.from_
            if top is not None and top < self.salary:
                return False
        return True


def build_search(filters: SearchFilters, modified_from: datetime | None = None) -> TrudvsemSearch:
    keywords = parse_query(filters.text)
    city: str | None = None
    if not filters.areas or AREA_RUSSIA in filters.areas:
        paths = ["/vacancies"]
    else:
        regions = [REGIONS[a] for a in filters.areas if a in REGIONS]
        paths = [f"/vacancies/region/{code}" for code, _ in regions]
        # Один город — проверяем его в адресе (регион шире города)
        city = regions[0][1] if len(regions) == 1 else None
    extra: QueryItems = []
    if modified_from is not None:
        extra.append(("modifiedFrom", modified_from.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")))
    queries = [
        (path, [("text", term), *extra])
        for term in (keywords.terms if keywords else [])
        for path in paths
    ]
    return TrudvsemSearch(
        queries=queries,
        keywords=keywords,
        city=city,
        remote_only=filters.work_format == ["REMOTE"],
        no_remote=bool(filters.work_format) and "REMOTE" not in filters.work_format,
        experience=set(filters.experience),
        salary=filters.salary,
        only_with_salary=filters.only_with_salary,
    )


class TrudvsemSource:
    portal = Portal.TRUDVSEM
    name = "api"

    def __init__(self, client: TrudvsemClient) -> None:
        self._client = client

    async def _search(self, search: TrudvsemSearch) -> list[VacancyShort]:
        found: dict[str, VacancyShort] = {}
        for path, params in search.queries:
            for raw in await self._client.search(path, params):
                try:
                    vacancy = vacancy_from_api(raw)
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning("trudvsem_item_skipped", error=repr(exc))
                    continue
                if search.matches(raw, vacancy):
                    found[vacancy.id] = vacancy
        return sorted(found.values(), key=lambda v: v.published_at, reverse=True)

    async def search_new(self, filters: SearchFilters, date_from: datetime) -> list[VacancyShort]:
        found = await self._search(build_search(filters, date_from))
        return [v for v in found if v.published_at >= date_from]

    async def search_all(self, filters: SearchFilters) -> list[VacancyShort]:
        return await self._search(build_search(filters))


class TrudvsemDetailsSource:
    """Вакансия по ОГРН и uuid из ссылки; не нашлась — снята с публикации."""

    name = "api"

    def __init__(self, client: TrudvsemClient) -> None:
        self._client = client

    async def get_details(self, vacancy_id: str, url: str | None = None) -> VacancyDetails | None:
        ids = card_ids(url or "")
        if ids is None:
            raise ValueError(f"не разобрать ссылку на вакансию «Работы России»: {url!r}")
        raw = await self._client.vacancy(*ids)
        return details_from_api(raw) if raw else None
