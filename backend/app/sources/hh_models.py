"""Pydantic-модели ответов api.hh.ru.

Описаны только поля, которые нужны сервису. Остальные поля сохраняются (extra="allow"),
поэтому model_dump(by_alias=True) возвращает полный ответ — он пишется в vacancies.raw.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class HHModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class IdName(HHModel):
    id: str
    name: str


class Salary(HHModel):
    from_: int | None = Field(default=None, alias="from")
    to: int | None = None
    currency: str | None = None
    gross: bool | None = None


class Employer(HHModel):
    # У анонимных работодателей id нет
    id: str | None = None
    name: str
    alternate_url: str | None = None


class VacancyShort(HHModel):
    """Вакансия из выдачи поиска GET /vacancies."""

    id: str
    name: str
    area: IdName
    salary: Salary | None = None
    employer: Employer
    published_at: datetime
    created_at: datetime | None = None
    archived: bool = False
    alternate_url: str
    experience: IdName | None = None
    schedule: IdName | None = None
    work_format: list[IdName] = Field(default_factory=list)


class KeySkill(HHModel):
    name: str


class Vacancy(VacancyShort):
    """Полная вакансия GET /vacancies/{id}."""

    description: str = ""
    key_skills: list[KeySkill] = Field(default_factory=list)
    employment: IdName | None = None


class VacancySearchPage(HHModel):
    items: list[VacancyShort]
    found: int
    pages: int
    page: int
    per_page: int


class ProfessionalRole(HHModel):
    id: str
    name: str


class ProfessionalRoleCategory(HHModel):
    id: str
    name: str
    roles: list[ProfessionalRole]


class Area(HHModel):
    id: str
    name: str
    parent_id: str | None = None
    areas: list["Area"] = Field(default_factory=list)
