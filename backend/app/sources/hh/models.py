"""Pydantic-модели ответов api.hh.ru, которых нет в общем формате (app.sources.models)."""

from pydantic import Field

from app.sources.models import IdName, SourceModel, VacancyShort


class KeySkill(SourceModel):
    name: str


class Vacancy(VacancyShort):
    """Полная вакансия GET /vacancies/{id}."""

    description: str = ""
    key_skills: list[KeySkill] = Field(default_factory=list)
    employment: IdName | None = None


class VacancySearchPage(SourceModel):
    items: list[VacancyShort]
    found: int
    pages: int
    page: int
    per_page: int


class ProfessionalRole(SourceModel):
    id: str
    name: str


class ProfessionalRoleCategory(SourceModel):
    id: str
    name: str
    roles: list[ProfessionalRole]


class Area(SourceModel):
    id: str
    name: str
    parent_id: str | None = None
    areas: list["Area"] = Field(default_factory=list)
