from collections.abc import Sequence
from datetime import datetime
from typing import Any

from app.sources.base import SearchFilters
from app.sources.hh_models import VacancyShort


def make_vacancy(
    vacancy_id: str,
    name: str = "Frontend-разработчик",
    *,
    published_at: datetime,
    employer_id: str | None = "1740",
) -> VacancyShort:
    return VacancyShort.model_validate(
        {
            "id": vacancy_id,
            "name": name,
            "area": {"id": "1", "name": "Москва"},
            "employer": {"id": employer_id, "name": f"Компания {employer_id}"},
            "published_at": published_at.isoformat(),
            "alternate_url": f"https://hh.ru/vacancy/{vacancy_id}",
            "work_format": [{"id": "REMOTE", "name": "Удалённо"}],
        }
    )


class FakeSource:
    """VacancySource для тестов: ответы по filters.text, запоминает запросы."""

    name = "api"

    def __init__(self, responses: dict[str | None, Sequence[VacancyShort] | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[SearchFilters, datetime]] = []

    async def search_new(self, filters: SearchFilters, date_from: datetime) -> list[VacancyShort]:
        self.calls.append((filters, date_from))
        response = self.responses.get(filters.text, [])
        if isinstance(response, Exception):
            raise response
        return list(response)


class FakeLock:
    def __init__(self, held: set[str], name: str) -> None:
        self._held = held
        self.name = name

    async def acquire(self, blocking: bool | None = None) -> bool:
        if self.name in self._held:
            return False
        self._held.add(self.name)
        return True

    async def release(self) -> None:
        self._held.discard(self.name)


class FakeRedis:
    """Подмена ArqRedis: блокировки в памяти, поставленные задачи запоминаются."""

    def __init__(self) -> None:
        self.held: set[str] = set()
        self.enqueued: list[tuple[str, dict[str, Any]]] = []

    def lock(self, name: str, **_: Any) -> FakeLock:
        return FakeLock(self.held, name)

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> None:
        self.enqueued.append((function, kwargs))
