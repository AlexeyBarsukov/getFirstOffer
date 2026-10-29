"""getmatch.ru: JSON-API сайта без ключа (`/api/offers`).

Сервер не применяет фильтры и сортирует не по дате, а вакансий немного (~900 IT-вакансий
за 30 дней), поэтому забираем ленту целиком (10 запросов по 100) и фильтруем у себя.
Лента кэшируется на минуту: в одном цикле её делят все подписки.
"""

import asyncio
import time
from datetime import datetime
from typing import Any

import httpx
import structlog

from app.ratelimit import Clock, Sleep
from app.sources.base import SearchFilters
from app.sources.details import VacancyDetails
from app.sources.getmatch.filters import build_search
from app.sources.getmatch.parsing import BASE_URL, details_from_api, vacancy_from_api
from app.sources.http import HHAPIError, RetryingClient
from app.sources.models import VacancyShort
from app.sources.portals import Portal, split_vacancy_id

logger = structlog.get_logger(__name__)

PER_PAGE = 100
# Страховка от бесконечного листания, если API начнёт отдавать полные страницы
MAX_PAGES = 30
FEED_TTL = 60.0


class GetmatchClient:
    def __init__(
        self,
        user_agent: str,
        base_url: str = BASE_URL,
        min_request_interval: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = RetryingClient(
            base_url,
            {"User-Agent": user_agent},
            min_request_interval,
            max_retries=2,
            timeout=30.0,
            transport=transport,
            sleep=sleep,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def feed(self) -> list[dict[str, Any]]:
        offers: list[dict[str, Any]] = []
        for page in range(MAX_PAGES):
            response = await self._http.get(
                "/api/offers", [("offset", str(page * PER_PAGE)), ("limit", str(PER_PAGE))]
            )
            batch = response.json().get("offers") or []
            offers += batch
            if len(batch) < PER_PAGE:
                break
        return offers

    async def offer(self, external_id: str) -> dict[str, Any]:
        data: dict[str, Any] = (await self._http.get(f"/api/offers/{external_id}")).json()
        return data


class GetmatchSource:
    """FullSearchSource getmatch. Новые и полная выдача — из одной и той же ленты."""

    portal = Portal.GETMATCH
    name = "api"

    def __init__(
        self, client: GetmatchClient, ttl: float = FEED_TTL, clock: Clock = time.monotonic
    ) -> None:
        self._client = client
        self._ttl = ttl
        self._clock = clock
        self._cache: tuple[float, list[dict[str, Any]]] | None = None
        self._lock = asyncio.Lock()

    async def _feed(self) -> list[dict[str, Any]]:
        async with self._lock:
            if self._cache is None or self._clock() - self._cache[0] >= self._ttl:
                self._cache = (self._clock(), await self._client.feed())
            return self._cache[1]

    async def search_all(self, filters: SearchFilters) -> list[VacancyShort]:
        search = build_search(filters)
        found = []
        for raw in await self._feed():
            if raw.get("offer_type") != "vacancy" or not search.matches(raw):
                continue
            try:
                found.append(vacancy_from_api(raw))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("getmatch_item_skipped", error=repr(exc))
        return sorted(found, key=lambda v: v.published_at, reverse=True)

    async def search_new(self, filters: SearchFilters, date_from: datetime) -> list[VacancyShort]:
        return [v for v in await self.search_all(filters) if v.published_at >= date_from]


class GetmatchDetailsSource:
    name = "api"

    def __init__(self, client: GetmatchClient) -> None:
        self._client = client

    async def get_details(self, vacancy_id: str, url: str | None = None) -> VacancyDetails | None:
        _, external_id = split_vacancy_id(vacancy_id)
        try:
            raw = await self._client.offer(external_id)
        except HHAPIError as exc:
            if exc.status_code == 404:
                return None
            raise
        return details_from_api(raw)
