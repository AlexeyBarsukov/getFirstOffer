import time
from datetime import datetime

from app.ratelimit import Clock
from app.sources.base import FullSearchSource, SearchFilters
from app.sources.hh.fallback import API_RECHECK_INTERVAL, ApiFallback
from app.sources.models import VacancyShort
from app.sources.portals import Portal


class AutoSource:
    """Поиск через API hh.ru, а при 403 — через RSS-ленту (см. ApiFallback).

    name — источник последнего успешного запроса (fetcher читает его после search_new).
    """

    portal = Portal.HH

    def __init__(
        self,
        api: FullSearchSource,
        rss: FullSearchSource,
        recheck_interval: float = API_RECHECK_INTERVAL,
        clock: Clock = time.monotonic,
    ) -> None:
        self._api = api
        self._rss = rss
        self._fallback = ApiFallback("search", recheck_interval, clock)
        self.name = api.name

    async def search_new(self, filters: SearchFilters, date_from: datetime) -> list[VacancyShort]:
        result, via_api = await self._fallback.call(
            lambda: self._api.search_new(filters, date_from),
            lambda: self._rss.search_new(filters, date_from),
        )
        self.name = self._api.name if via_api else self._rss.name
        return result

    async def search_all(self, filters: SearchFilters) -> list[VacancyShort]:
        result, via_api = await self._fallback.call(
            lambda: self._api.search_all(filters),
            lambda: self._rss.search_all(filters),
        )
        self.name = self._api.name if via_api else self._rss.name
        return result
