from datetime import UTC, datetime

import pytest

from app.sources.base import SearchFilters
from app.sources.hh.auto import AutoSource
from app.sources.http import HHAPIError
from app.sources.models import VacancyShort
from app.sources.portals import Portal
from tests.factories import make_vacancy

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
FILTERS = SearchFilters(text="react")


class StubSource:
    portal = Portal.HH

    def __init__(self, name: str, errors: list[Exception] | None = None) -> None:
        self.name = name
        self.errors = errors or []
        self.calls = 0

    async def search_new(self, filters: SearchFilters, date_from: datetime) -> list[VacancyShort]:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return [make_vacancy(f"{self.name}-{self.calls}", published_at=NOW)]

    async def search_all(self, filters: SearchFilters) -> list[VacancyShort]:
        return await self.search_new(filters, NOW)


def forbidden() -> HHAPIError:
    return HHAPIError(403, ["forbidden"], "req")


async def test_uses_api_while_it_works() -> None:
    api, rss = StubSource("api"), StubSource("rss")
    source = AutoSource(api, rss)

    found = await source.search_new(FILTERS, NOW)

    assert [v.id for v in found] == ["api-1"]
    assert source.name == "api"
    assert rss.calls == 0


async def test_falls_back_to_rss_on_403_and_rechecks_later() -> None:
    now = [0.0]
    api, rss = StubSource("api", [forbidden(), forbidden()]), StubSource("rss")
    source = AutoSource(api, rss, recheck_interval=1800, clock=lambda: now[0])

    first = await source.search_new(FILTERS, NOW)
    assert [v.id for v in first] == ["rss-1"]
    assert source.name == "rss"

    # В течение интервала API не дёргаем
    now[0] = 1000
    await source.search_new(FILTERS, NOW)
    assert api.calls == 1

    # Интервал прошёл — пробуем снова, 403 → опять RSS
    now[0] = 1801
    await source.search_new(FILTERS, NOW)
    assert (api.calls, rss.calls) == (2, 3)

    # API заработал (например, появился токен) — возвращаемся на него
    now[0] = 3700
    found = await source.search_new(FILTERS, NOW)
    assert [v.id for v in found] == ["api-3"]
    assert source.name == "api"


async def test_other_api_errors_are_not_masked() -> None:
    api = StubSource("api", [HHAPIError(500, [], None)])
    source = AutoSource(api, StubSource("rss"))

    with pytest.raises(HHAPIError):
        await source.search_new(FILTERS, NOW)
