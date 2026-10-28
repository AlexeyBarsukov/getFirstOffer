"""Источник вакансий: страница поиска на сайте hh.ru (встроенный JSON HH-Lux-InitialState).

До 100 вакансий на страницу и листание, у работодателя настоящий id, зарплата числами.
Но страница тяжёлая (~1–2 МБ), поэтому её читаем только когда не хватает RSS
(см. SiteSource) — например, при подгрузке вакансий за 24 часа по широкой подписке.
"""

import json
import re
from datetime import datetime
from html import unescape
from typing import Any

import httpx
import structlog

from app.sources.base import SearchFilters
from app.sources.hh.api import build_search_params
from app.sources.http import QueryItems, RetryingClient
from app.sources.models import VacancyShort
from app.sources.portals import Portal

logger = structlog.get_logger(__name__)

SEARCH_PATH = "/search/vacancy"
ITEMS_PER_PAGE = 100
# Для поиска новых больше 500 за раз не нужно; для полной подборки — вся выдача
# (hh.ru показывает не больше 2000 результатов)
MAX_PAGES = 5
MAX_PAGES_FULL = 20

_INITIAL_STATE = re.compile(r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', re.S)

# Названия как в справочнике hh.ru (/dictionaries) — для карточки вакансии
EXPERIENCE_NAMES = {
    "noExperience": "Нет опыта",
    "between1And3": "От 1 года до 3 лет",
    "between3And6": "От 3 до 6 лет",
    "moreThan6": "Более 6 лет",
}
WORK_FORMAT_NAMES = {
    "ON_SITE": "На месте работодателя",
    "REMOTE": "Удалённо",
    "HYBRID": "Гибрид",
    "FIELD_WORK": "Разъездной",
}


def build_page_params(filters: SearchFilters, page: int) -> QueryItems:
    params = [p for p in build_search_params(filters) if p[0] not in ("page", "per_page")]
    return [*params, ("items_on_page", str(ITEMS_PER_PAGE)), ("page", str(page))]


def _work_formats(raw: Any) -> list[str]:
    # На странице формат записан как [{"workFormatsElement": ["REMOTE"]}]
    result: list[str] = []
    for item in raw or []:
        if isinstance(item, dict):
            result += [str(v) for v in item.get("workFormatsElement", [])]
        elif isinstance(item, str):
            result.append(item)
    return result


def _parse_item(item: dict[str, Any]) -> VacancyShort:
    vacancy_id = str(item["vacancyId"])
    company = item.get("company") or {}
    compensation = item.get("compensation") or {}
    salary = None
    if compensation.get("from") is not None or compensation.get("to") is not None:
        salary = {
            "from": compensation.get("from"),
            "to": compensation.get("to"),
            "currency": compensation.get("currencyCode"),
            "gross": compensation.get("gross"),
        }
    experience = item.get("workExperience")
    area = item.get("area") or {}
    return VacancyShort.model_validate(
        {
            "id": vacancy_id,
            "name": item["name"],
            "area": {"id": str(area.get("id", "")), "name": area.get("name", "")},
            "salary": salary,
            "employer": {
                "id": str(company["id"]) if company.get("id") else None,
                "name": company.get("visibleName") or company.get("name") or "Компания не указана",
            },
            "published_at": datetime.fromisoformat(item["publicationTime"]["$"]),
            "alternate_url": f"https://hh.ru/vacancy/{vacancy_id}",
            "experience": {"id": experience, "name": EXPERIENCE_NAMES.get(experience, experience)}
            if experience
            else None,
            "work_format": [
                {"id": f, "name": WORK_FORMAT_NAMES.get(f, f)}
                for f in _work_formats(item.get("workFormats"))
            ],
        }
    )


def parse_search_page(html: str) -> tuple[list[VacancyShort], int]:
    """Вакансии со страницы и общее число найденных."""
    match = _INITIAL_STATE.search(html)
    if not match:
        raise ValueError("на странице поиска нет HH-Lux-InitialState (капча?)")
    result = json.loads(unescape(match.group(1))).get("vacancySearchResult") or {}
    vacancies = []
    for item in result.get("vacancies") or []:
        try:
            vacancies.append(_parse_item(item))
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("search_page_item_skipped", error=repr(exc))
    return vacancies, int(result.get("totalResults") or 0)


class HHSearchPageSource:
    portal = Portal.HH
    name = "parser"

    def __init__(
        self,
        user_agent: str,
        base_url: str = "https://hh.ru",
        min_request_interval: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = RetryingClient(
            base_url,
            {"User-Agent": user_agent},
            min_request_interval,
            max_retries=2,
            timeout=20.0,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _pages(
        self, filters: SearchFilters, max_pages: int, date_from: datetime | None
    ) -> list[VacancyShort]:
        found: list[VacancyShort] = []
        seen: set[str] = set()
        for page in range(max_pages):
            response = await self._http.get(SEARCH_PATH, build_page_params(filters, page))
            items, total = parse_search_page(response.text)
            for vacancy in items:
                # Между страницами выдача может сдвинуться — без дублей
                if vacancy.id not in seen and (
                    date_from is None or vacancy.published_at >= date_from
                ):
                    seen.add(vacancy.id)
                    found.append(vacancy)
            # Выдача отсортирована по дате: дальше только более старые вакансии
            reached_older = date_from is not None and any(v.published_at < date_from for v in items)
            if not items or reached_older or (page + 1) * ITEMS_PER_PAGE >= total:
                break
        else:
            logger.warning("search_page_limit_reached", pages=max_pages, found=len(found))
        return found

    async def search_new(self, filters: SearchFilters, date_from: datetime) -> list[VacancyShort]:
        return await self._pages(filters, MAX_PAGES, date_from)

    async def search_all(self, filters: SearchFilters) -> list[VacancyShort]:
        return await self._pages(filters, MAX_PAGES_FULL, None)
