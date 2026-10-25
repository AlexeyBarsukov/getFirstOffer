from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, Field

from app.sources.hh_models import VacancyShort


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
    """Источник вакансий: API hh.ru или парсер сайта."""

    name: str

    async def search_new(self, filters: SearchFilters, date_from: datetime) -> list[VacancyShort]:
        """Вакансии по фильтрам, опубликованные после date_from, сначала свежие."""
        ...
