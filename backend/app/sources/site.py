from datetime import datetime

import structlog

from app.sources.base import FullSearchSource, SearchFilters
from app.sources.hh_models import VacancyShort
from app.sources.hh_rss import HHRssSource

logger = structlog.get_logger(__name__)


class SiteSource:
    """Поиск через сайт hh.ru без API: RSS-лента, а если её не хватило — страница поиска.

    RSS лёгкий (~20 КБ), но отдаёт только 20 последних вакансий. Если все 20 новее окна
    поиска, часть могла не поместиться — тогда дочитываем страницу поиска (с листанием).
    Обычно это подгрузка за 24 часа по широкой подписке; при опросе раз в минуту хватает RSS.

    name — источник последнего ответа.
    """

    def __init__(self, rss: HHRssSource, page: FullSearchSource) -> None:
        self._rss = rss
        self._page = page
        self.name = rss.name

    async def search_new(self, filters: SearchFilters, date_from: datetime) -> list[VacancyShort]:
        found, window_full = await self._rss.search_with_status(filters, date_from)
        self.name = self._rss.name
        if not window_full:
            return found
        try:
            result = await self._page.search_new(filters, date_from)
        except Exception:
            # Страница не прочиталась (капча, вёрстка) — отдаём хотя бы то, что было в RSS
            logger.exception("search_page_failed_using_rss_only")
            return found
        logger.info("rss_window_full_used_search_page", rss=len(found), page=len(result))
        self.name = self._page.name
        return result

    async def search_all(self, filters: SearchFilters) -> list[VacancyShort]:
        # RSS тут бесполезен (20 записей) — сразу страница поиска
        self.name = self._page.name
        return await self._page.search_all(filters)
