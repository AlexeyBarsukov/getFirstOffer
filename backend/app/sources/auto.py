import time
from datetime import datetime

import structlog

from app.ratelimit import Clock
from app.sources.base import SearchFilters, VacancySource
from app.sources.hh_models import VacancyShort
from app.sources.http import HHAPIError

logger = structlog.get_logger(__name__)

# Как часто снова пробовать API после 403
API_RECHECK_INTERVAL = 30 * 60


class AutoSource:
    """API hh.ru, пока он отвечает; при 403 — RSS-лента. API перепроверяется раз в 30 минут.

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
        self._recheck_interval = recheck_interval
        self._clock = clock
        self._api_blocked_until: float | None = None
        self.name = api.name

    async def search_new(self, filters: SearchFilters, date_from: datetime) -> list[VacancyShort]:
        blocked = self._api_blocked_until is not None
        if not blocked or self._clock() >= (self._api_blocked_until or 0):
            try:
                result = await self._api.search_new(filters, date_from)
            except HHAPIError as exc:
                if exc.status_code != 403:
                    raise
                if not blocked:
                    logger.warning("hh_api_forbidden_using_rss", error=str(exc))
                self._api_blocked_until = self._clock() + self._recheck_interval
            else:
                if blocked:
                    logger.info("hh_api_available_again")
                self._api_blocked_until = None
                self.name = self._api.name
                return result

        result = await self._rss.search_new(filters, date_from)
        self.name = self._rss.name
        return result
