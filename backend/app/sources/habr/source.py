"""Хабр Карьера: поиск вакансий и детали для трекера.

Новые вакансии — RSS-лента поиска (лёгкая, до 50 записей, без зарплаты и листания). Если
все 50 записей новее окна поиска, часть могла не поместиться — дочитываем страницу поиска.
Полная выдача для подборки — страницы поиска (25 вакансий на страницу).
"""

import asyncio
from datetime import datetime

import httpx
import structlog

from app.ratelimit import Sleep
from app.sources.base import SearchFilters
from app.sources.details import VacancyDetails
from app.sources.habr.filters import HabrSearch, build_search
from app.sources.habr.parsing import (
    BASE_URL,
    parse_feed,
    parse_search_page,
    parse_vacancy_page,
    published_before,
)
from app.sources.http import HHAPIError, QueryItems, RetryingClient
from app.sources.models import VacancyShort
from app.sources.portals import Portal, split_vacancy_id

logger = structlog.get_logger(__name__)

RSS_PATH = "/vacancies/rss"
SEARCH_PATH = "/vacancies"
RSS_FEED_SIZE = 50
# Листаем страницу поиска не дальше: для новых — 5 страниц (125 вакансий), для подборки — 20
MAX_PAGES = 5
MAX_PAGES_FULL = 20


class HabrClient:
    """HTTP к career.habr.com: не чаще раза в 2 секунды, повторы при 429/5xx."""

    def __init__(
        self,
        user_agent: str,
        base_url: str = BASE_URL,
        min_request_interval: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = RetryingClient(
            base_url,
            {"User-Agent": user_agent},
            min_request_interval,
            max_retries=2,
            transport=transport,
            sleep=sleep,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def rss(self, params: QueryItems) -> list[VacancyShort]:
        return parse_feed((await self._http.get(RSS_PATH, params)).text)

    async def search_page(self, params: QueryItems, page: int) -> tuple[list[VacancyShort], int]:
        response = await self._http.get(SEARCH_PATH, [*params, ("page", str(page))])
        return parse_search_page(response.text)

    async def vacancy_page(self, external_id: str) -> str:
        return (await self._http.get(f"/vacancies/{external_id}")).text


def _merge(found: list[VacancyShort], search: HabrSearch) -> list[VacancyShort]:
    """Без повторов (одна вакансия находится по нескольким словам), подходящие, свежие первыми."""
    unique = {v.id: v for v in found}
    return sorted(
        (v for v in unique.values() if search.matches(v)),
        key=lambda v: v.published_at,
        reverse=True,
    )


class HabrSource:
    """FullSearchSource Хабр Карьеры. name — способ последнего ответа: rss или parser."""

    portal = Portal.HABR

    def __init__(self, client: HabrClient) -> None:
        self._client = client
        self.name = "rss"

    async def _pages(
        self, params: QueryItems, max_pages: int, date_from: datetime | None
    ) -> list[VacancyShort]:
        found: list[VacancyShort] = []
        page, total_pages = 1, 1
        while page <= min(total_pages, max_pages):
            items, total_pages = await self._client.search_page(params, page)
            found += items
            # Выдача отсортирована по дате: дошли до старых — дальше листать незачем
            if not items or (date_from and published_before(items, date_from)):
                break
            page += 1
        return found

    async def search_new(self, filters: SearchFilters, date_from: datetime) -> list[VacancyShort]:
        search = build_search(filters)
        found: list[VacancyShort] = []
        self.name = "rss"
        for params in search.queries:
            items = await self._client.rss(params)
            fresh = [v for v in items if v.published_at >= date_from]
            if len(items) >= RSS_FEED_SIZE and len(fresh) == len(items):
                logger.info("habr_rss_window_full_using_search_page", params=params)
                fresh = await self._pages(params, MAX_PAGES, date_from)
                self.name = "parser"
            found += [v for v in fresh if v.published_at >= date_from]
        return _merge(found, search)

    async def search_all(self, filters: SearchFilters) -> list[VacancyShort]:
        search = build_search(filters)
        self.name = "parser"
        found: list[VacancyShort] = []
        for params in search.queries:
            found += await self._pages(params, MAX_PAGES_FULL, None)
        return _merge(found, search)


class HabrDetailsSource:
    """Страница вакансии career.habr.com/vacancies/{id}; 404 — вакансию удалили."""

    name = "page"

    def __init__(self, client: HabrClient) -> None:
        self._client = client

    async def get_details(self, vacancy_id: str) -> VacancyDetails | None:
        _, external_id = split_vacancy_id(vacancy_id)
        try:
            html = await self._client.vacancy_page(external_id)
        except HHAPIError as exc:
            if exc.status_code == 404:
                return None
            raise
        return parse_vacancy_page(html)
