import httpx
import pytest

from app.sources.details import VacancyDetails
from app.sources.hh.api import HHClient
from app.sources.hh.details import (
    ApiDetailsSource,
    AutoDetailsSource,
    PageDetailsSource,
    parse_vacancy_page,
)
from app.sources.http import HHAPIError
from tests.conftest import FIXTURES, FakeSleep, load_fixture

# Сокращённые настоящие страницы hh.ru (только встроенный JSON вакансии)
OPEN_PAGE = (FIXTURES / "hh" / "vacancy_page_open.html").read_text(encoding="utf-8")
ARCHIVED_PAGE = (FIXTURES / "hh" / "vacancy_page_archived.html").read_text(encoding="utf-8")


def test_parse_open_vacancy_page() -> None:
    details = parse_vacancy_page(OPEN_PAGE)

    assert details == VacancyDetails(
        id="138760315",
        name="Frontend-разработчик",
        archived=False,
        employer_id="13069293",
        salary={"from": 20000, "to": None, "currency": "RUR", "gross": True},
        experience="between1And3",
        work_format=["REMOTE"],
        area="Москва",
    )


def test_parse_archived_vacancy_page() -> None:
    details = parse_vacancy_page(ARCHIVED_PAGE)

    assert details.id == "120000000"
    assert details.archived is True
    # Базовая ставка, а не сумма за вахту (perModeFrom)
    assert details.salary is not None
    assert details.salary["from"] == 6266


def test_parse_page_without_state_fails() -> None:
    with pytest.raises(ValueError):
        parse_vacancy_page("<html>капча</html>")


def test_snapshot_is_order_independent() -> None:
    a = VacancyDetails(id="1", name="x", archived=False, work_format=["REMOTE", "HYBRID"])
    b = a.model_copy(update={"work_format": ["HYBRID", "REMOTE"]})

    assert a.snapshot("page") == b.snapshot("page")
    assert a.snapshot("page")["source"] == "page"


def page_source(handler: httpx.MockTransport) -> PageDetailsSource:
    return PageDetailsSource("ua", transport=handler, min_request_interval=0)


async def test_page_source_404_means_deleted() -> None:
    source = page_source(httpx.MockTransport(lambda _: httpx.Response(404)))

    assert await source.get_details("1") is None
    await source.aclose()


async def test_page_source_403_is_an_error_not_deleted() -> None:
    source = page_source(httpx.MockTransport(lambda _: httpx.Response(403)))

    with pytest.raises(HHAPIError):
        await source.get_details("1")
    await source.aclose()


async def test_page_source_reads_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/vacancy/138760315"
        return httpx.Response(200, text=OPEN_PAGE)

    source = page_source(httpx.MockTransport(handler))
    details = await source.get_details("138760315")
    await source.aclose()

    assert details is not None
    assert details.name == "Frontend-разработчик"


async def test_api_source_maps_vacancy(fake_sleep: FakeSleep) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json=load_fixture("hh/vacancy_archived.json"))
    )
    async with HHClient("ua", min_request_interval=0, transport=transport, sleep=fake_sleep) as c:
        details = await ApiDetailsSource(c).get_details("110000001")

    assert details is not None
    assert details.archived is True
    assert details.salary == {"from": 100000, "to": 150000, "currency": "RUR", "gross": False}
    assert (details.experience, details.work_format, details.area) == (
        "noExperience",
        ["REMOTE"],
        "Москва",
    )


class StubDetails:
    def __init__(self, name: str, error: Exception | None = None) -> None:
        self.name = name
        self.error = error
        self.calls = 0

    async def get_details(self, vacancy_id: str) -> VacancyDetails | None:
        self.calls += 1
        if self.error:
            raise self.error
        return VacancyDetails(id=vacancy_id, name=self.name, archived=False)


async def test_auto_details_falls_back_to_page_on_403() -> None:
    api = StubDetails("api", HHAPIError(403, ["forbidden"], None))
    page = StubDetails("page")
    source = AutoDetailsSource(api, page)

    first = await source.get_details("1")
    await source.get_details("2")

    assert first is not None
    assert first.name == "page"
    assert source.name == "page"
    assert (api.calls, page.calls) == (1, 2)
