import time
from datetime import datetime

from app.ratelimit import Clock
from app.sources.base import SearchFilters, VacancySource
from app.sources.fallback import API_RECHECK_INTERVAL, ApiFallback
from app.sources.hh_models import VacancyShort


class AutoSource:
    """Поиск через API hh.ru, а при 403 — через RSS-ленту (см. ApiFallback).

    name — источник последнего успешного запроса (fetcher читает его после search_new).
    """

    def __init__(
        self,
        api: VacancySource,
        rss: VacancySource,
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
