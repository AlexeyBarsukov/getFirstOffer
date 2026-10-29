from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.services.presets import PRESETS
from app.sources.base import SearchFilters
from app.sources.trudvsem.parsing import (
    card_ids,
    experience_code,
    is_quota_only,
    vacancies,
    vacancy_from_api,
)
from app.sources.trudvsem.source import (
    TrudvsemClient,
    TrudvsemDetailsSource,
    TrudvsemSource,
    build_search,
)
from tests.conftest import FakeSleep, load_fixture

# Настоящие ответы /api/v1/vacancies по «front-end» и «frontend» (29.10.2026), без контактов
RECORDED: dict[str, Any] = load_fixture("trudvsem/vacancies.json")
RAW = {v["id"][:8]: v for v in vacancies(RECORDED)}


def test_parse_recorded_vacancy() -> None:
    vacancy = vacancy_from_api(RAW["bd615f60"])

    assert vacancy.id == "td-" + RAW["bd615f60"]["id"].replace("-", "")
    assert vacancy.name == "Senior/Middle+ Frontend разработчик (HR-платформа Пульс)"
    assert vacancy.employer.name == "Сбербанк России, ПАО"
    assert vacancy.area.name == "Город Москва"
    assert vacancy.salary is not None
    assert (vacancy.salary.from_, vacancy.salary.to, vacancy.salary.currency) == (
        340000,
        None,
        "RUR",
    )
    assert vacancy.experience is not None and vacancy.experience.id == "between3And6"
    assert vacancy.published_at.tzinfo is not None
    assert vacancy.alternate_url.startswith("https://trudvsem.ru/vacancy/card/")


def test_remote_and_quota() -> None:
    remote_quota = RAW["9f64459e"]

    assert [f.id for f in vacancy_from_api(remote_quota).work_format] == ["REMOTE"]
    assert is_quota_only(remote_quota)
    assert not is_quota_only(RAW["bd615f60"])


@pytest.mark.parametrize(
    ("years", "code"),
    [(None, None), (0, "noExperience"), (1, "between1And3"), (3, "between3And6"), (6, "moreThan6")],
)
def test_experience_code(years: int | None, code: str | None) -> None:
    assert experience_code(years) == code


def test_card_ids() -> None:
    url = "https://trudvsem.ru/vacancy/card/1027702005891/1a0e3e18-aa97-11f0-b9af-bdc82d0d5dcd"

    assert card_ids(url) == ("1027702005891", "1a0e3e18-aa97-11f0-b9af-bdc82d0d5dcd")
    assert card_ids("https://trudvsem.ru/") is None


def matching(filters: SearchFilters) -> list[str]:
    search = build_search(filters)
    return sorted(
        raw["job-name"] for raw in RAW.values() if search.matches(raw, vacancy_from_api(raw))
    )


def test_frontend_presets() -> None:
    # Удалённые «Программист Front-end» — только по квоте, их не берём
    assert matching(PRESETS["frontend_all"].filters) == []
    assert matching(SearchFilters(text=PRESETS["frontend_all"].filters.text)) == [
        "Frontend-разработчик (React + вёрстка)",
        "Senior/Middle+ Frontend разработчик (HR-платформа Пульс)",
    ]


def test_city_checks_address() -> None:
    kazan = build_search(SearchFilters(text="NAME:frontend", areas=["88"]))

    assert kazan.queries == [("/vacancies/region/1600000000000", [("text", "frontend")])]
    assert kazan.city == "Казань"


def test_experience_filter() -> None:
    filters = SearchFilters(text="NAME:frontend", experience=["noExperience"])

    assert matching(filters) == ["Frontend-разработчик (React + вёрстка)"]


async def test_search_is_paged_and_cached() -> None:
    page0 = {"results": {"vacancies": [{"vacancy": RAW["bd615f60"]}] * 100}}
    page1 = RECORDED
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=page0 if request.url.params["offset"] == "0" else page1)

    now = [0.0]
    client = TrudvsemClient(
        "test", transport=httpx.MockTransport(handler), sleep=FakeSleep(), clock=lambda: now[0]
    )
    source = TrudvsemSource(client)
    filters = SearchFilters(text="NAME:frontend")

    found = await source.search_all(filters)
    await source.search_all(filters)  # из кэша
    assert len(requests) == 2
    assert {v.name for v in found} == {
        "Frontend-разработчик (React + вёрстка)",
        "Senior/Middle+ Frontend разработчик (HR-платформа Пульс)",
    }

    since = datetime(2026, 10, 28, tzinfo=UTC)
    await source.search_new(filters, since)
    assert requests[-1].url.params["modifiedFrom"] == "2026-10-28T00:00:00Z"

    now[0] = 601
    await source.search_all(filters)
    assert len(requests) == 6


async def test_details_by_card_url() -> None:
    raw = RAW["bd615f60"]
    url = raw["vac_url"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(raw["id"]):
            return httpx.Response(200, json={"results": {"vacancies": [{"vacancy": raw}]}})
        return httpx.Response(200, json={"meta": {"total": 0}, "results": {}})

    client = TrudvsemClient("test", transport=httpx.MockTransport(handler), sleep=FakeSleep())
    details = TrudvsemDetailsSource(client)

    found = await details.get_details("td-x", url)
    assert found is not None and found.name.startswith("Senior/Middle+")
    gone = url.replace(raw["id"], "00000000-0000-0000-0000-000000000000")
    assert await details.get_details("td-x", gone) is None
    with pytest.raises(ValueError):
        await details.get_details("td-x", None)
