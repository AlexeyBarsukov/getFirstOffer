"""Текущее состояние одной вакансии — для отслеживания изменений.

Общий формат для всех площадок; источники деталей лежат в пакете площадки
(например, app.sources.hh.details).
"""

from typing import Any, Protocol

from pydantic import BaseModel, Field


class VacancyDetails(BaseModel):
    id: str
    name: str
    archived: bool
    employer_id: str | None = None
    # {"from", "to", "currency", "gross"} — тот же формат, что в payload событий salary_changed
    salary: dict[str, Any] | None = None
    experience: str | None = None
    work_format: list[str] = Field(default_factory=list)
    area: str | None = None

    def snapshot(self, source: str) -> dict[str, Any]:
        """Отслеживаемые поля. source нужен, чтобы не сравнивать снимки из разных источников."""
        return {
            "source": source,
            "name": self.name,
            "archived": self.archived,
            "salary": self.salary,
            "experience": self.experience,
            "work_format": sorted(self.work_format),
            "area": self.area,
        }


def salary_dict(
    salary_from: int | None, salary_to: int | None, currency: str | None, gross: bool | None
) -> dict[str, Any] | None:
    if salary_from is None and salary_to is None:
        return None
    return {"from": salary_from, "to": salary_to, "currency": currency, "gross": gross}


class VacancyDetailsSource(Protocol):
    name: str

    async def get_details(self, vacancy_id: str) -> VacancyDetails | None:
        """Текущее состояние вакансии или None, если её удалили (404)."""
        ...
