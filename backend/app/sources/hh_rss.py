"""Источник вакансий через публичную RSS-ленту поиска hh.ru (без токена).

https://hh.ru/search/vacancy/rss?<те же фильтры, что в поиске на сайте>

Ограничения по сравнению с API:
- лента отдаёт только 20 последних вакансий, листание страниц не поддерживается;
- у работодателя нет id — используем стабильный ключ из хэша названия (employer_key);
- зарплата — текстом («от 100 000 до 150 000 ₽»), признака «до вычета налогов» нет.
"""

import asyncio
import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from html import unescape
from types import TracebackType
from typing import Any, Self

import httpx
import structlog

from app.config import Settings
from app.ratelimit import Sleep
from app.sources.base import SearchFilters
from app.sources.hh_api import build_search_params
from app.sources.hh_models import VacancyShort
from app.sources.http import QueryItems, RetryingClient

logger = structlog.get_logger(__name__)

RSS_PATH = "/search/vacancy/rss"
RSS_FEED_SIZE = 20

CURRENCY_CODES = {"₽": "RUR", "руб.": "RUR", "$": "USD", "€": "EUR", "₸": "KZT", "Br": "BYR"}

_VACANCY_ID = re.compile(r"/vacancy/(\d+)")
_DESCRIPTION_FIELD = re.compile(r"<p>\s*([^:<]+?)\s*:\s*(.*?)\s*</p>", re.S)
_AMOUNT = r"(\d[\d ]*)"


def build_rss_params(filters: SearchFilters) -> QueryItems:
    # Лента принимает те же параметры, что и поиск; листания и date_from в ней нет
    return [p for p in build_search_params(filters) if p[0] not in ("page", "per_page")]


def employer_key(name: str) -> str:
    """Стабильный «id» работодателя по названию (в RSS настоящего id нет).

    Короткий, чтобы поместиться в callback_data кнопки «Скрыть компанию» (64 байта).
    """
    digest = hashlib.sha1(name.strip().casefold().encode()).hexdigest()[:12]
    return f"n{digest}"


def parse_salary_text(text: str) -> dict[str, Any] | None:
    """«от 100 000 до 150 000 ₽» → {"from": 100000, "to": 150000, "currency": "RUR"}."""
    text = unescape(text).replace(" ", " ").strip()
    salary_from = re.search(rf"от\s*{_AMOUNT}", text)
    salary_to = re.search(rf"до\s*{_AMOUNT}", text)
    if not salary_from and not salary_to:
        return None
    currency = next((code for sign, code in CURRENCY_CODES.items() if text.endswith(sign)), None)
    return {
        "from": int(salary_from.group(1).replace(" ", "")) if salary_from else None,
        "to": int(salary_to.group(1).replace(" ", "")) if salary_to else None,
        "currency": currency,
        "gross": None,
    }


def _parse_item(item: ET.Element) -> VacancyShort:
    link = (item.findtext("link") or "").strip()
    match = _VACANCY_ID.search(link)
    if not match:
        raise ValueError(f"нет id вакансии в ссылке {link!r}")
    fields = dict(_DESCRIPTION_FIELD.findall(item.findtext("description") or ""))
    employer = unescape(fields.get("Вакансия компании", "")).strip() or "Компания не указана"
    region = unescape(fields.get("Регион", "")).strip()
    salary = parse_salary_text(fields.get("Предполагаемый уровень месячного дохода", ""))
    return VacancyShort.model_validate(
        {
            "id": match.group(1),
            "name": unescape(item.findtext("title") or "").strip(),
            "area": {"id": "", "name": region},
            "salary": salary,
            "employer": {"id": employer_key(employer), "name": employer},
            "published_at": datetime.fromisoformat((item.findtext("pubDate") or "").strip()),
            "alternate_url": f"https://hh.ru/vacancy/{match.group(1)}",
        }
    )


def parse_feed(xml_text: str) -> list[VacancyShort]:
    root = ET.fromstring(xml_text)
    vacancies = []
    for item in root.iterfind("./channel/item"):
        try:
            vacancies.append(_parse_item(item))
        except ValueError as exc:
            # Одна странная запись не должна ломать всю ленту
            logger.warning("rss_item_skipped", error=str(exc))
    return vacancies


class HHRssSource:
    """VacancySource поверх RSS-ленты поиска hh.ru."""

    name = "rss"

    def __init__(
        self,
        user_agent: str,
        base_url: str = "https://hh.ru",
        min_request_interval: float = 1.0,
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

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(user_agent=settings.hh_user_agent)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def fetch(self, filters: SearchFilters) -> list[VacancyShort]:
        response = await self._http.get(RSS_PATH, build_rss_params(filters))
        return parse_feed(response.text)

    async def search_new(self, filters: SearchFilters, date_from: datetime) -> list[VacancyShort]:
        items = await self.fetch(filters)
        fresh = [v for v in items if v.published_at >= date_from]
        if len(items) >= RSS_FEED_SIZE and len(fresh) == len(items):
            # Все 20 записей новее окна поиска — часть вакансий могла не поместиться в ленту
            logger.warning("rss_feed_window_full", date_from=date_from.isoformat())
        return fresh
