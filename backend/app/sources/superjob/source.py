"""SuperJob: официальный API (https://api.superjob.ru), ключ приложения в X-Api-App-Id.

Лимит API — 120 запросов в минуту с одного IP; не больше 500 вакансий на один запрос.
"""

import asyncio
from datetime import datetime
from typing import Any

import httpx
import structlog

from app.ratelimit import Sleep
from app.sources.base import SearchFilters
from app.sources.details import VacancyDetails
from app.sources.http import HHAPIError, QueryItems, RetryingClient
from app.sources.models import VacancyShort
from app.sources.portals import Portal, split_vacancy_id
from app.sources.superjob.filters import PER_PAGE, SuperJobSearch, build_search
from app.sources.superjob.parsing import details_from_api, vacancy_from_api

logger = structlog.get_logger(__name__)

BASE_URL = "https://api.superjob.ru/2.0"
MAX_RESULTS = 500
# Для новых вакансий хватает первой страницы (100), при подгрузке за сутки — с запасом
MAX_PAGES_NEW = 2


class SuperJobClient:
    def __init__(
        self,
        api_key: str,
        user_agent: str,
        base_url: str = BASE_URL,
        min_request_interval: float = 0.6,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = RetryingClient(
            base_url,
            {"X-Api-App-Id": api_key, "User-Agent": user_agent},
            min_request_interval,
            transport=transport,
            sleep=sleep,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def vacancies(self, params: QueryItems, page: int) -> dict[str, Any]:
        response = await self._http.get(
            "/vacancies/", [*params, ("page", str(page)), ("count", str(PER_PAGE))]
        )
        data: dict[str, Any] = response.json()
        return data

    async def vacancy(self, external_id: str) -> dict[str, Any]:
        data: dict[str, Any] = (await self._http.get(f"/vacancies/{external_id}/")).json()
        return data


class SuperJobSource:
    """FullSearchSource SuperJob."""

    portal = Portal.SUPERJOB
    name = "api"

    def __init__(self, client: SuperJobClient) -> None:
        self._client = client

    async def _search(
        self, search: SuperJobSearch, extra: QueryItems, max_pages: int
    ) -> list[VacancyShort]:
        if search.params is None:
            return []
        found: list[VacancyShort] = []
        for page in range(min(max_pages, MAX_RESULTS // PER_PAGE)):
            data = await self._client.vacancies([*search.params, *extra], page)
            for raw in data.get("objects") or []:
                try:
                    found.append(vacancy_from_api(raw))
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning("superjob_item_skipped", error=repr(exc))
            if not data.get("more"):
                break
        unique = {v.id: v for v in found if search.matches(v)}
        return sorted(unique.values(), key=lambda v: v.published_at, reverse=True)

    async def search_new(self, filters: SearchFilters, date_from: datetime) -> list[VacancyShort]:
        extra: QueryItems = [("date_published_from", str(int(date_from.timestamp())))]
        found = await self._search(build_search(filters), extra, MAX_PAGES_NEW)
        return [v for v in found if v.published_at >= date_from]

    async def search_all(self, filters: SearchFilters) -> list[VacancyShort]:
        return await self._search(build_search(filters), [], MAX_RESULTS // PER_PAGE)


class SuperJobDetailsSource:
    name = "api"

    def __init__(self, client: SuperJobClient) -> None:
        self._client = client

    async def get_details(self, vacancy_id: str, url: str | None = None) -> VacancyDetails | None:
        _, external_id = split_vacancy_id(vacancy_id)
        try:
            raw = await self._client.vacancy(external_id)
        except HHAPIError as exc:
            if exc.status_code == 404:
                return None
            raise
        return details_from_api(raw)
