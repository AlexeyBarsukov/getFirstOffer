from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.services.presets import PRESETS
from app.sources.base import SearchFilters
from app.sources.getmatch.filters import build_search
from app.sources.getmatch.parsing import vacancy_from_api
from app.sources.getmatch.source import (
    GetmatchClient,
    GetmatchDetailsSource,
    GetmatchSource,
)
from tests.conftest import FakeSleep, load_fixture

# Настоящие вакансии из https://getmatch.ru/api/offers (29.10.2026): весь фронтенд ленты,
# несколько с зарплатой и с Казанью (без описаний)
RECORDED: list[dict[str, Any]] = load_fixture("getmatch/offers.json")["offers"]
BY_ID = {o["id"]: o for o in RECORDED}


def test_parse_recorded_offer() -> None:
    vacancy = vacancy_from_api(BY_ID[28400])

    assert vacancy.id == "gm-28400"
    assert vacancy.name == "Junior Frontend Developer (React)"
    assert vacancy.employer.name == "Т-Банк"
    assert vacancy.area.name == "Москва, Санкт-Петербург"
    assert [f.id for f in vacancy.work_format] == ["HYBRID"]
    assert (
        vacancy.alternate_url
        == "https://getmatch.ru/vacancies/28400-junior-frontend-developer-react"
    )
    assert vacancy.published_at.tzinfo is not None


def test_parse_salary_and_hidden_company() -> None:
    salary = vacancy_from_api(BY_ID[36523]).salary
    hidden = vacancy_from_api(BY_ID[36829])

    assert salary is not None
    assert (salary.from_, salary.to, salary.currency, salary.gross) == (
        120000,
        175000,
        "RUR",
        False,
    )
    assert hidden.salary is None
    assert (hidden.employer.id, hidden.employer.name) == (None, "Компания скрыта")


def matching(filters: SearchFilters) -> list[str]:
    search = build_search(filters)
    return sorted(o["position"] for o in RECORDED if search.matches(o))


def test_remote_frontend_preset() -> None:
    assert matching(PRESETS["frontend_all"].filters) == [
        "Middle+ / Senior Frontend Developer (Angular)",
        "Senior Fullstack Engineer (React / Node / real-time)",
        "Ведущий фулстек-разработчик (Python + Vue 3)",
        "Разработчик React",
    ]


def test_city_and_format() -> None:
    kazan_hybrid = SearchFilters(text="NAME:(data OR ml)", areas=["88"], work_format=["HYBRID"])
    moscow_office = SearchFilters(text="NAME:frontend", areas=["1"], work_format=["ON_SITE"])

    assert matching(kazan_hybrid) == [
        "Data Scientist Fast Track",
        "ML-инженер  (Центр технологий искусственного интеллекта)",
    ]
    assert matching(moscow_office) == []  # во фронтенде Москвы только гибрид


def test_salary_filters() -> None:
    assert matching(SearchFilters(text="тестировщик", salary=200000)) == []
    assert matching(SearchFilters(text="тестировщик", salary=150000)) == ["Ручной тестировщик"]
    assert matching(SearchFilters(text="NAME:react", only_with_salary=True)) == []


def test_roles_without_keywords_are_not_searched() -> None:
    assert matching(PRESETS["support"].filters) == []


def make_client(handler: Any) -> GetmatchClient:
    return GetmatchClient("test", transport=httpx.MockTransport(handler), sleep=FakeSleep())


async def test_feed_is_paged_and_cached() -> None:
    pages = {"0": [{**RECORDED[0], "id": i} for i in range(100)], "100": RECORDED}
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"offers": pages[request.url.params["offset"]]})

    now = [0.0]
    source = GetmatchSource(make_client(handler), ttl=60, clock=lambda: now[0])

    found = await source.search_all(SearchFilters(text="NAME:react", work_format=["REMOTE"]))
    await source.search_all(SearchFilters(text="NAME:vue"))  # из кэша
    assert len(requests) == 2
    assert {v.id for v in found} >= {"gm-35813", "gm-0", "gm-99"}

    now[0] = 61
    await source.search_all(SearchFilters(text="NAME:vue"))
    assert len(requests) == 4


async def test_search_new_uses_publication_time() -> None:
    published = datetime.fromisoformat(BY_ID[35813]["published_at"]).replace(tzinfo=UTC)
    source = GetmatchSource(make_client(lambda _: httpx.Response(200, json={"offers": RECORDED})))
    filters = SearchFilters(text="NAME:react")

    assert [
        v.id for v in await source.search_new(filters, published - timedelta(milliseconds=100))
    ] == ["gm-35813"]


async def test_details() -> None:
    closed = {**BY_ID[35813], "is_active": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/offers/1":
            return httpx.Response(404, json={"detail": "Vacancy not found"})
        return httpx.Response(200, json=closed)

    details = GetmatchDetailsSource(make_client(handler))

    found = await details.get_details("gm-35813")
    assert found is not None and found.archived is True
    assert await details.get_details("gm-1") is None
