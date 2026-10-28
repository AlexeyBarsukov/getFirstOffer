from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, Field

from app.sources.models import VacancyShort
from app.sources.portals import Portal


class SearchFilters(BaseModel):
    """Фильтры подписки, которые передаются в поиск источника.

    Локальные фильтры (стоп-слова, чёрный список работодателей) сюда не входят —
    они применяются после получения вакансий.
    """

    text: str | None = None
    professional_roles: list[str] = Field(default_factory=list)
    areas: list[str] = Field(default_factory=list)
    experience: list[str] = Field(default_factory=list)
    work_format: list[str] = Field(default_factory=list)
    salary: int | None = None
    only_with_salary: bool = False


class VacancySource(Protocol):
    """Источник вакансий одной площадки.

    portal — площадка; name — способ получения последнего ответа (api, rss, parser).
    """

    portal: Portal
    name: str

    async def search_new(self, filters: SearchFilters, date_from: datetime) -> list[VacancyShort]:
        """Вакансии по фильтрам, опубликованные после date_from, сначала свежие."""
        ...


class FullSearchSource(VacancySource, Protocol):
    """Источник, который умеет отдать ВСЮ выдачу по фильтрам (для подборки)."""

    async def search_all(self, filters: SearchFilters) -> list[VacancyShort]:
        """Все вакансии по фильтрам (в пределах лимита глубины выдачи площадки)."""
        ...
