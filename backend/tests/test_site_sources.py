import json
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

import httpx
import pytest

from app.services.presets import PRESETS
from app.sources.hh_models import VacancyShort
from app.sources.hh_search_page import (
    SEARCH_PATH,
    HHSearchPageSource,
    build_page_params,
    parse_search_page,
)
from app.sources.site import SiteSource
from tests.conftest import FIXTURES
from tests.factories import make_vacancy

# Сокращённая настоящая страница поиска: техподдержка, удалёнка, за 24 часа — 84 вакансии
RECORDED_PAGE = (FIXTURES / "hh" / "search_page.html").read_text(encoding="utf-8")
MSK = timezone(timedelta(hours=3))
FILTERS = PRESETS["support"].filters


def make_page(items: list[dict[str, Any]], total: int) -> str:
    state = {"vacancySearchResult": {"totalResults": total, "vacancies": items}}
    return f'<template id="HH-Lux-InitialState">{escape(json.dumps(state))}</template>'


def page_item(vacancy_id: int, published: datetime) -> dict[str, Any]:
    return {
        "vacancyId": vacancy_id,
        "name": f"Вакансия {vacancy_id}",
        "area": {"id": 1, "name": "Москва"},
        "company": {"id": 100, "visibleName": "Ромашка"},
        "publicationTime": {"$": published.isoformat()},
    }


def test_parse_recorded_search_page() -> None:
    vacancies, total = parse_search_page(RECORDED_PAGE)

    assert (len(vacancies), total) == (84, 84)
    first = vacancies[0]
    assert first.id == "138766085"
    assert first.name == "Специалист поддержки пользователей"
    assert (first.employer.id, first.employer.name) == ("12462549", "Петров Дмитрий Сергеевич")
    assert first.salary is not None
    assert (first.salary.from_, first.salary.to, first.salary.currency, first.salary.gross) == (
        45000,
        55000,
        "RUR",
        False,
    )
    assert first.area.name == "Златоуст"
    assert first.experience is not None and first.experience.id == "noExperience"
    assert [(f.id, f.name) for f in first.work_format] == [("REMOTE", "Удалённо")]
    dates = [v.published_at for v in vacancies]
    assert dates == sorted(dates, reverse=True)


def test_parse_page_without_state_fails() -> None:
    with pytest.raises(ValueError, match="капча"):
        parse_search_page("<html>Подтвердите, что вы не робот</html>")


def test_build_page_params() -> None:
    params = build_page_params(FILTERS, page=2)

    assert ("items_on_page", "100") in params
    assert ("page", "2") in params
    assert [k for k, _ in params].count("page") == 1
    assert ("professional_role", "121") in params


async def test_search_page_paginates_until_older_vacancies() -> None:
    date_from = datetime(2026, 10, 25, 0, 0, tzinfo=MSK)
    newest = datetime(2026, 10, 25, 23, 0, tzinfo=MSK)
    pages = {
        "0": [page_item(i, newest - timedelta(minutes=i)) for i in range(100)],
        "1": [
            page_item(100, newest - timedelta(hours=5)),
            page_item(101, date_from - timedelta(hours=1)),
        ],
    }
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == SEARCH_PATH
        page = request.url.params["page"]
        requested.append(page)
        return httpx.Response(200, text=make_page(pages[page], total=500))

    source = HHSearchPageSource(
        "ua", transport=httpx.MockTransport(handler), min_request_interval=0
    )
    found = await source.search_new(FILTERS, date_from)
    await source.aclose()

    assert requested == ["0", "1"]
    assert len(found) == 101
    assert "101" not in {v.id for v in found}


class StubRss:
    name = "rss"

    def __init__(self, found: list[VacancyShort], window_full: bool) -> None:
        self.found = found
        self.window_full = window_full

    async def search_with_status(
        self, filters: Any, date_from: datetime
    ) -> tuple[list[VacancyShort], bool]:
        return self.found, self.window_full


class StubPage:
    name = "parser"

    def __init__(self, result: list[VacancyShort] | Exception) -> None:
        self.result = result
        self.calls = 0

    async def search_new(self, filters: Any, date_from: datetime) -> list[VacancyShort]:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


NOW = datetime(2026, 10, 25, 12, 0, tzinfo=MSK)


async def test_site_uses_rss_when_it_is_enough() -> None:
    rss = StubRss([make_vacancy("1", published_at=NOW)], window_full=False)
    page = StubPage([])
    site = SiteSource(rss, page)  # type: ignore[arg-type]

    found = await site.search_new(FILTERS, NOW)

    assert [v.id for v in found] == ["1"]
    assert (site.name, page.calls) == ("rss", 0)


async def test_site_reads_search_page_when_rss_is_full() -> None:
    rss = StubRss([make_vacancy(str(i), published_at=NOW) for i in range(20)], window_full=True)
    page = StubPage([make_vacancy(str(i), published_at=NOW) for i in range(84)])
    site = SiteSource(rss, page)  # type: ignore[arg-type]

    found = await site.search_new(FILTERS, NOW)

    assert len(found) == 84
    assert site.name == "parser"


async def test_site_falls_back_to_rss_if_page_fails() -> None:
    rss = StubRss([make_vacancy(str(i), published_at=NOW) for i in range(20)], window_full=True)
    site = SiteSource(rss, StubPage(ValueError("капча")))  # type: ignore[arg-type]

    found = await site.search_new(FILTERS, NOW)

    assert len(found) == 20
    assert site.name == "rss"
