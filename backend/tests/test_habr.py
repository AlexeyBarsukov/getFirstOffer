import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from html import escape
from typing import Any

import httpx
import pytest

from app.services.presets import PRESETS
from app.sources.base import SearchFilters
from app.sources.habr.filters import build_search
from app.sources.habr.parsing import parse_feed, parse_search_page, parse_vacancy_page
from app.sources.habr.source import HabrClient, HabrDetailsSource, HabrSource
from app.sources.models import VacancyShort
from tests.conftest import FIXTURES, FakeSleep

# Настоящие ответы career.habr.com (28.10.2026): RSS по «react», поиск по «frontend»
# (сортировка по дате, 31 вакансия, 2 страницы), страница вакансии 1000168870
RECORDED_FEED = (FIXTURES / "habr" / "rss_search.xml").read_text(encoding="utf-8")
RECORDED_PAGE = (FIXTURES / "habr" / "search_page.html").read_text(encoding="utf-8")
RECORDED_VACANCY = (FIXTURES / "habr" / "vacancy_page.html").read_text(encoding="utf-8")
MSK = timezone(timedelta(hours=3))
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=MSK)


def test_parse_recorded_feed() -> None:
    vacancies = parse_feed(RECORDED_FEED)

    assert len(vacancies) == 45
    first = vacancies[1]
    assert first.id == "habr-1000169548"
    assert first.name == "Frontend-разработчик (SPA, React, REST / GraphQL API)"
    assert first.employer.name == "ELEMENTAREE"
    assert first.area.name == "Москва"
    assert first.salary is not None
    assert (first.salary.from_, first.salary.to, first.salary.currency) == (170000, None, "RUR")
    assert first.experience is not None and first.experience.name == "Middle"
    assert [f.id for f in first.work_format] == ["REMOTE"]
    assert first.published_at == datetime(2026, 10, 28, 8, 47, 7, tzinfo=MSK)
    assert first.alternate_url == "https://career.habr.com/vacancies/1000169548"


def test_parse_recorded_search_page() -> None:
    vacancies, total_pages = parse_search_page(RECORDED_PAGE)

    assert (len(vacancies), total_pages) == (25, 2)
    first = vacancies[0]
    assert first.id == "habr-1000125058"
    assert first.employer.name == "Greenway Global"
    assert first.salary is not None
    assert (first.salary.from_, first.salary.to, first.salary.currency) == (250000, 400000, "RUR")
    assert first.experience is not None and first.experience.id == "lead"
    dates = [v.published_at for v in vacancies]
    assert dates == sorted(dates, reverse=True)


def test_parse_recorded_vacancy_page() -> None:
    details = parse_vacancy_page(RECORDED_VACANCY)

    assert details.id == "habr-1000168870"
    assert details.name == "Разработчик Frontend (AI)"
    assert details.archived is False
    assert details.salary is None
    assert details.work_format == ["REMOTE"]
    assert details.area == "Москва, Санкт-Петербург, Казань"


def test_parse_archived_vacancy_page() -> None:
    start = RECORDED_VACANCY.index('data-ssr-state="true">') + len('data-ssr-state="true">')
    end = RECORDED_VACANCY.index("</script>", start)
    state = json.loads(RECORDED_VACANCY[start:end])
    state["vacancy"]["archived"] = True
    html = RECORDED_VACANCY[:start] + json.dumps(state) + RECORDED_VACANCY[end:]

    assert parse_vacancy_page(html).archived is True


def test_catalog_presets() -> None:
    remote = build_search(PRESETS["frontend_all"].filters)
    kazan = build_search(PRESETS["frontend_kazan"].filters)

    assert len(remote.queries) == 7  # по запросу на слово
    assert ("remote", "true") in remote.queries[0]
    assert not any(k == "city_id" for k, _ in remote.queries[0])
    assert ("city_id", "698") in kazan.queries[0]
    assert not any(k == "remote" for k, _ in kazan.queries[0])


def test_roles_without_keywords_are_not_searched() -> None:
    assert build_search(PRESETS["support"].filters).queries == []


def vacancy(name: str, *, qualification: str | None = None, remote: bool = True) -> VacancyShort:
    return VacancyShort.model_validate(
        {
            "id": "habr-1",
            "name": name,
            "area": {"id": "", "name": ""},
            "employer": {"id": "n1", "name": "Ромашка"},
            "published_at": NOW,
            "alternate_url": "https://career.habr.com/vacancies/1",
            "experience": {"id": qualification, "name": qualification} if qualification else None,
            "work_format": [{"id": "REMOTE", "name": "Удалённо"}] if remote else [],
        }
    )


def test_local_filters() -> None:
    search = build_search(
        SearchFilters(
            text="NAME:(react OR vue)",
            work_format=["REMOTE"],
            experience=["between1And3"],
        )
    )

    assert search.matches(vacancy("React-разработчик", qualification="middle"))
    assert search.matches(vacancy("Vue developer"))  # квалификация не указана — оставляем
    assert not search.matches(vacancy("QA-инженер"))  # слово только в описании
    assert not search.matches(vacancy("React-разработчик", qualification="lead"))
    assert not search.matches(vacancy("React-разработчик", remote=False))


# --- Источник поверх HTTP ---


def rss_item(vacancy_id: int, name: str, published: datetime) -> str:
    return (
        f"<item><title>{escape(f'Требуется «{name}»')}</title>"
        f"<description>Можно удалённо.</description><author>Ромашка</author>"
        f"<pubDate>{format_datetime(published)}</pubDate>"
        f"<link>https://career.habr.com/vacancies/{vacancy_id}</link>"
        f"<guid>{vacancy_id}</guid></item>"
    )


def make_feed(items: list[str]) -> str:
    return f"<rss><channel>{''.join(items)}</channel></rss>"


def page_item(vacancy_id: int, name: str, published: datetime) -> dict[str, Any]:
    return {
        "id": vacancy_id,
        "href": f"/vacancies/{vacancy_id}",
        "title": name,
        "remoteWork": True,
        "publishedDate": {"date": published.isoformat()},
        "company": {"id": 7, "title": "Ромашка"},
        "salary": {"from": 200000, "to": None, "currency": "rur"},
        "locations": [],
    }


def make_page(items: list[dict[str, Any]], total_pages: int) -> str:
    state = {"vacancies": {"list": items, "meta": {"totalPages": total_pages}}}
    return (
        '<script type="application/json" data-ssr-state="true">'
        f"{escape(json.dumps(state))}</script>"
    )


def make_source(handler: Any) -> tuple[HabrSource, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response: httpx.Response = handler(request)
        return response

    client = HabrClient("test", transport=httpx.MockTransport(record), sleep=FakeSleep())
    return HabrSource(client), requests


FILTERS = SearchFilters(text="NAME:(react OR vue)", work_format=["REMOTE"])


async def test_search_new_uses_rss_per_keyword() -> None:
    feeds = {
        "react": [
            rss_item(1, "React-разработчик", NOW),
            rss_item(2, "Java-разработчик", NOW),  # react только в описании
            rss_item(3, "React старый", NOW - timedelta(days=2)),
        ],
        "vue": [rss_item(1, "React-разработчик", NOW), rss_item(4, "Vue", NOW)],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/vacancies/rss"
        assert request.url.params["remote"] == "true"
        return httpx.Response(200, text=make_feed(feeds[request.url.params["q"]]))

    source, requests = make_source(handler)

    found = await source.search_new(FILTERS, NOW - timedelta(hours=1))

    assert sorted(v.id for v in found) == ["habr-1", "habr-4"]
    assert len(requests) == 2
    assert source.name == "rss"


async def test_full_rss_window_falls_back_to_search_page() -> None:
    feed = make_feed([rss_item(i, "React", NOW) for i in range(50)])
    pages = {
        "1": make_page([page_item(i, "React", NOW) for i in range(100, 125)], 3),
        "2": make_page(
            [page_item(200, "React", NOW), page_item(201, "React", NOW - timedelta(days=1))], 3
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/vacancies/rss":
            return httpx.Response(200, text=feed)
        return httpx.Response(200, text=pages[request.url.params["page"]])

    source, requests = make_source(handler)

    found = await source.search_new(SearchFilters(text="NAME:react"), NOW - timedelta(hours=1))

    # 25 + 1 свежая со второй страницы; на третью не идём — дошли до старых
    assert len(found) == 26
    assert found[0].salary is not None  # со страницы поиска — с зарплатой
    assert [r.url.params.get("page") for r in requests] == [None, "1", "2"]
    assert source.name == "parser"


async def test_search_all_reads_every_page() -> None:
    pages = {
        "1": make_page([page_item(1, "React", NOW)], 2),
        "2": make_page([page_item(2, "React", NOW - timedelta(days=30))], 2),
    }
    source, requests = make_source(lambda r: httpx.Response(200, text=pages[r.url.params["page"]]))

    found = await source.search_all(SearchFilters(text="NAME:react"))

    assert [v.id for v in found] == ["habr-1", "habr-2"]
    assert len(requests) == 2


@pytest.mark.parametrize(("status", "expected_none"), [(404, True), (200, False)])
async def test_details_source(status: int, expected_none: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/vacancies/1000168870"
        return httpx.Response(status, text=RECORDED_VACANCY)

    client = HabrClient("test", transport=httpx.MockTransport(handler), sleep=FakeSleep())

    details = await HabrDetailsSource(client).get_details("habr-1000168870")

    assert (details is None) == expected_none
