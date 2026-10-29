from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.services.presets import PRESETS
from app.sources.base import SearchFilters
from app.sources.superjob.filters import build_search
from app.sources.superjob.parsing import details_from_api, is_remote, vacancy_from_api
from app.sources.superjob.source import SuperJobClient, SuperJobDetailsSource, SuperJobSource
from tests.conftest import FakeSleep, load_fixture

# Настоящий ответ GET /2.0/vacancies/ (29.10.2026): фронтенд в названии по всей России,
# без контактов и описаний компаний
RECORDED: dict[str, Any] = load_fixture("superjob/vacancies.json")
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def test_parse_recorded_vacancies() -> None:
    vacancies = [vacancy_from_api(raw) for raw in RECORDED["objects"]]

    assert len(vacancies) == 6
    by_id = {v.id: v for v in vacancies}
    pik = by_id["sj-52229075"]
    assert pik.name == "Фронтенд разработчик/UX|UI дизайнер"
    assert (pik.employer.name, pik.area.name) == ("ПИК", "Москва")
    assert pik.salary is None  # 0 — «не указана»
    assert pik.experience is not None and pik.experience.id == "between1And3"
    assert [f.id for f in pik.work_format] == ["REMOTE"]
    assert pik.alternate_url.startswith("https://www.superjob.ru/vakansii/")
    # «гибридный формат (часть времени в офисе, часть — удаленно)» — не удалёнка
    assert by_id["sj-52192677"].work_format == []
    assert by_id["sj-52187149"].name == "Mod Developer (Python & React)"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Полную удалёнку или гибрид на выбор", True),
        ("Возможность удаленной работы или работы в офисе", True),
        ("Возможность работать из дома", True),
        ("Работа полностью удаленно", True),
        ("Внимание! Никакой удаленной работы!", False),
        ("Формат: офис, без удалёнки", False),
        ("Настройка удаленного доступа, удаленная поддержка пользователей", False),
    ],
)
def test_is_remote(text: str, expected: bool) -> None:
    assert is_remote({"work": text}) is expected


def test_place_of_work_at_home_is_remote() -> None:
    assert is_remote({"place_of_work": {"id": 2}})


def test_salary_and_anonymous_employer() -> None:
    raw = {
        **RECORDED["objects"][0],
        "payment_from": 150000,
        "payment_to": 0,
        "currency": "rub",
        "anonymous": True,
    }

    vacancy = vacancy_from_api(raw)

    assert vacancy.salary is not None
    assert (vacancy.salary.from_, vacancy.salary.to, vacancy.salary.currency) == (
        150000,
        None,
        "RUR",
    )
    assert vacancy.employer.id is None


def test_catalog_presets() -> None:
    remote = build_search(PRESETS["frontend_all"].filters)
    kazan = build_search(PRESETS["frontend_kazan"].filters)

    assert remote.params is not None and kazan.params is not None
    assert ("keywords[0][srws]", "1") in remote.params  # в названии
    assert ("c", "1") in remote.params
    assert remote.remote_only
    assert ("t[]", "55") in kazan.params
    assert not kazan.remote_only


def test_roles_without_keywords_use_catalogues() -> None:
    search = build_search(PRESETS["support"].filters)

    assert search.params is not None
    assert ("catalogues", "57") in search.params


def test_unknown_city_is_not_searched() -> None:
    assert build_search(SearchFilters(text="react", areas=["1438"])).params is None


def make_source(pages: list[dict[str, Any]]) -> tuple[SuperJobSource, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=pages[int(request.url.params["page"])])

    client = SuperJobClient(
        "key", "test", transport=httpx.MockTransport(handler), sleep=FakeSleep()
    )
    return SuperJobSource(client), requests


def raw_vacancy(vacancy_id: int, name: str, published: datetime) -> dict[str, Any]:
    return {
        **RECORDED["objects"][4],
        "id": vacancy_id,
        "profession": name,
        "date_published": int(published.timestamp()),
    }


async def test_search_new_filters_title_and_window() -> None:
    page = {
        "objects": [
            raw_vacancy(1, "React-разработчик", NOW),
            raw_vacancy(2, "Аналитик данных", NOW),
            raw_vacancy(3, "Vue-разработчик", NOW - timedelta(days=2)),
        ],
        "more": False,
    }
    source, requests = make_source([page])

    found = await source.search_new(
        SearchFilters(text="NAME:(react OR vue)"), NOW - timedelta(hours=1)
    )

    assert [v.id for v in found] == ["sj-1"]
    request = requests[0]
    assert request.headers["X-Api-App-Id"] == "key"
    assert request.url.params["date_published_from"] == str(
        int((NOW - timedelta(hours=1)).timestamp())
    )
    assert request.url.params["keywords[0][keys]"] == "react vue"


async def test_search_all_pages_until_no_more() -> None:
    pages = [
        {"objects": [raw_vacancy(1, "React", NOW)], "more": True},
        {"objects": [raw_vacancy(2, "React", NOW - timedelta(days=3))], "more": False},
    ]
    source, requests = make_source(pages)

    found = await source.search_all(SearchFilters(text="NAME:react"))

    assert [v.id for v in found] == ["sj-1", "sj-2"]
    assert len(requests) == 2


async def test_details() -> None:
    archived = {**RECORDED["objects"][0], "is_archive": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/1/"):
            return httpx.Response(404, json={"error": {"code": 404}})
        return httpx.Response(200, json=archived)

    client = SuperJobClient(
        "key", "test", transport=httpx.MockTransport(handler), sleep=FakeSleep()
    )
    details = SuperJobDetailsSource(client)

    found = await details.get_details("sj-52192677")
    assert found is not None and found.archived is True
    assert await details.get_details("sj-1") is None
    assert details_from_api({**archived, "is_storage": True}) is None
