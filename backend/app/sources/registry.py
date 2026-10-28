"""Реестр площадок: какие площадки включены и их источники поиска и деталей.

Воркер собирает реестр при старте (build_portals) и кладёт в ctx["portals"].
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

from app.config import Settings
from app.sources.base import FullSearchSource
from app.sources.details import VacancyDetailsSource
from app.sources.habr.source import HabrClient, HabrDetailsSource, HabrSource
from app.sources.hh.api import HHApiSource, HHClient
from app.sources.hh.auto import AutoSource
from app.sources.hh.details import ApiDetailsSource, AutoDetailsSource, PageDetailsSource
from app.sources.hh.rss import HHRssSource
from app.sources.hh.search_page import HHSearchPageSource
from app.sources.hh.site import SiteSource
from app.sources.portals import Portal

logger = structlog.get_logger(__name__)


@dataclass
class PortalSources:
    search: FullSearchSource
    # Источник деталей для трекера изменений; None — изменения на площадке не отслеживаем
    details: VacancyDetailsSource | None = None
    # HTTP-клиенты, которые нужно закрыть при остановке воркера
    closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    async def aclose(self) -> None:
        for close in self.closers:
            await close()


def build_hh(settings: Settings) -> PortalSources:
    """hh.ru: API, а при 403 — сайт (RSS + страница поиска); детали — API или страница."""
    client = HHClient.from_settings(settings)
    rss = HHRssSource.from_settings(settings)
    search_page = HHSearchPageSource(settings.hh_user_agent)
    page = PageDetailsSource(settings.hh_user_agent)

    api = HHApiSource(client)
    site = SiteSource(rss, search_page)
    searches: dict[str, FullSearchSource] = {
        "api": api,
        "rss": site,
        "auto": AutoSource(api, site),
    }
    api_details = ApiDetailsSource(client)
    details: dict[str, VacancyDetailsSource] = {
        "api": api_details,
        "rss": page,
        "auto": AutoDetailsSource(api_details, page),
    }
    logger.info("vacancy_source", portal=Portal.HH, source=settings.hh_source)
    return PortalSources(
        search=searches[settings.hh_source],
        details=details[settings.hh_source],
        closers=[client.aclose, rss.aclose, search_page.aclose, page.aclose],
    )


def build_habr(settings: Settings) -> PortalSources:
    """Хабр Карьера: RSS + страница поиска, детали — страница вакансии. Ключ не нужен."""
    client = HabrClient(settings.hh_user_agent)
    return PortalSources(
        search=HabrSource(client),
        details=HabrDetailsSource(client),
        closers=[client.aclose],
    )


BUILDERS: dict[Portal, Callable[[Settings], PortalSources]] = {
    Portal.HH: build_hh,
    Portal.HABR: build_habr,
}


def build_portals(settings: Settings) -> dict[Portal, PortalSources]:
    portals: dict[Portal, PortalSources] = {}
    for portal in settings.enabled_portals:
        builder = BUILDERS.get(portal)
        if builder is None:
            logger.warning("portal_not_implemented", portal=portal)
            continue
        portals[portal] = builder(settings)
    return portals
