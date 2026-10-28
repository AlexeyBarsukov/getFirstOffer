from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest

from app.ratelimit import RateLimiter
from app.sources.base import SearchFilters
from app.sources.hh.api import (
    MAX_SEARCH_DEPTH,
    HHAPIError,
    HHApiSource,
    HHClient,
    build_search_params,
)
from tests.conftest import FakeSleep, load_fixture

USER_AGENT = "getFirstOffer/0.1 (test@example.com)"
Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler, sleep: FakeSleep, app_token: str | None = None) -> HHClient:
    return HHClient(
        user_agent=USER_AGENT,
        app_token=app_token,
        min_request_interval=0,
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )


def test_build_search_params_repeats_list_filters() -> None:
    filters = SearchFilters(
        text="NAME:react",
        professional_roles=["96", "121"],
        areas=["113"],
        experience=["noExperience", "between1And3"],
        work_format=["REMOTE"],
        salary=100000,
        only_with_salary=True,
    )
    date_from = datetime(2026, 10, 25, 12, 0, 5, 123, tzinfo=UTC)

    params = build_search_params(filters, date_from, page=2, per_page=50)

    assert params == [
        ("order_by", "publication_time"),
        ("page", "2"),
        ("per_page", "50"),
        ("text", "NAME:react"),
        ("professional_role", "96"),
        ("professional_role", "121"),
        ("area", "113"),
        ("experience", "noExperience"),
        ("experience", "between1And3"),
        ("work_format", "REMOTE"),
        ("salary", "100000"),
        ("only_with_salary", "true"),
        ("date_from", "2026-10-25T12:00:05+00:00"),
    ]


def test_build_search_params_skips_empty_filters() -> None:
    assert build_search_params(SearchFilters()) == [
        ("order_by", "publication_time"),
        ("page", "0"),
        ("per_page", "100"),
    ]


async def test_search_parses_response_and_sends_headers(fake_sleep: FakeSleep) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=load_fixture("hh/vacancies_page_0.json"))

    async with make_client(handler, fake_sleep, app_token="secret") as client:
        page = await client.search_vacancies(SearchFilters(areas=["113"]))

    request = requests[0]
    assert request.url.path == "/vacancies"
    assert request.url.params.get_list("area") == ["113"]
    assert request.headers["HH-User-Agent"] == USER_AGENT
    assert request.headers["Authorization"] == "Bearer secret"

    assert page.found == 3
    first, anonymous = page.items
    assert first.id == "110000001"
    assert first.employer.name == "Яндекс"
    assert first.salary is not None
    assert (first.salary.from_, first.salary.to, first.salary.currency) == (80000, 120000, "RUR")
    assert first.published_at == datetime(
        2026, 10, 25, 12, 1, 30, tzinfo=timezone(timedelta(hours=3))
    )
    assert [f.id for f in first.work_format] == ["REMOTE"]
    assert anonymous.salary is None
    assert anonymous.employer.id is None


async def test_no_authorization_header_without_token(fake_sleep: FakeSleep) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=load_fixture("hh/vacancies_page_1.json"))

    async with make_client(handler, fake_sleep) as client:
        await client.search_vacancies(SearchFilters())

    assert "Authorization" not in requests[0].headers


async def test_search_all_walks_all_pages(fake_sleep: FakeSleep) -> None:
    pages = {
        "0": load_fixture("hh/vacancies_page_0.json"),
        "1": load_fixture("hh/vacancies_page_1.json"),
    }
    requested_pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        requested_pages.append(page)
        return httpx.Response(200, json=pages[page])

    async with make_client(handler, fake_sleep) as client:
        source = HHApiSource(client)
        items = await source.search_new(SearchFilters(), datetime(2026, 10, 25, tzinfo=UTC))

    assert requested_pages == ["0", "1"]
    assert [v.id for v in items] == ["110000001", "110000002", "110000003"]


async def test_search_all_stops_at_depth_limit(fake_sleep: FakeSleep) -> None:
    item = load_fixture("hh/vacancies_page_1.json")["items"][0]
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        requested_pages.append(page)
        body = {"items": [item], "found": 50000, "pages": 500, "page": page, "per_page": 100}
        return httpx.Response(200, json=body)

    async with make_client(handler, fake_sleep) as client:
        await client.search_all(SearchFilters())

    assert requested_pages == list(range(MAX_SEARCH_DEPTH // 100))


async def test_get_vacancy_parses_archived_vacancy(fake_sleep: FakeSleep) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/vacancies/110000001"
        return httpx.Response(200, json=load_fixture("hh/vacancy_archived.json"))

    async with make_client(handler, fake_sleep) as client:
        vacancy = await client.get_vacancy("110000001")

    assert vacancy.archived is True
    assert vacancy.salary is not None
    assert vacancy.salary.from_ == 100000
    assert [s.name for s in vacancy.key_skills] == ["React", "TypeScript"]
    assert vacancy.employment is not None
    assert vacancy.employment.id == "full"


async def test_get_professional_roles_from_recorded_response(fake_sleep: FakeSleep) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load_fixture("hh/professional_roles.json"))

    async with make_client(handler, fake_sleep) as client:
        categories = await client.get_professional_roles()

    it = next(c for c in categories if c.id == "11")
    roles = {r.id: r.name for r in it.roles}
    assert roles["96"] == "Программист, разработчик"
    assert roles["121"] == "Специалист технической поддержки"


async def test_retries_on_429_using_retry_after(fake_sleep: FakeSleep) -> None:
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(503),
            httpx.Response(200, json=load_fixture("hh/vacancies_page_1.json")),
        ]
    )

    async with make_client(lambda _: next(responses), fake_sleep) as client:
        page = await client.search_vacancies(SearchFilters())

    assert page.found == 3
    # Retry-After из заголовка, затем экспоненциальная задержка 1.0 * 2**1
    assert fake_sleep.calls == [7.0, 2.0]


async def test_retries_on_network_error(fake_sleep: FakeSleep) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json=load_fixture("hh/vacancies_page_1.json"))

    async with make_client(handler, fake_sleep) as client:
        await client.search_vacancies(SearchFilters())

    assert calls == 2
    assert fake_sleep.calls == [1.0]


async def test_gives_up_after_max_retries(fake_sleep: FakeSleep) -> None:
    async with make_client(lambda _: httpx.Response(503), fake_sleep) as client:
        with pytest.raises(HHAPIError) as exc_info:
            await client.search_vacancies(SearchFilters())

    assert exc_info.value.status_code == 503
    assert fake_sleep.calls == [1.0, 2.0, 4.0]


async def test_forbidden_is_not_retried(fake_sleep: FakeSleep) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = {"errors": [{"type": "forbidden"}], "request_id": "abc123"}
        return httpx.Response(403, json=body)

    async with make_client(handler, fake_sleep) as client:
        with pytest.raises(HHAPIError) as exc_info:
            await client.search_vacancies(SearchFilters())

    assert calls == 1
    assert exc_info.value.error_types == ["forbidden"]
    assert exc_info.value.request_id == "abc123"


async def test_rate_limiter_waits_between_calls(fake_sleep: FakeSleep) -> None:
    now = [100.0]
    limiter = RateLimiter(0.5, sleep=fake_sleep, clock=lambda: now[0])

    await limiter.acquire()
    now[0] += 0.2
    await limiter.acquire()
    now[0] += 1.0
    await limiter.acquire()

    assert fake_sleep.calls == [pytest.approx(0.3)]


async def test_get_dictionaries_from_recorded_response(fake_sleep: FakeSleep) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load_fixture("hh/dictionaries.json"))

    async with make_client(handler, fake_sleep) as client:
        dictionaries = await client.get_dictionaries()

    assert "REMOTE" in {i.id for i in dictionaries["work_format"]}
    assert {"noExperience", "between1And3"} <= {i.id for i in dictionaries["experience"]}
    # currency устроен иначе ({code, abbr, ...}) и пропускается
    assert "currency" not in dictionaries
