"""Вакансия из выдачи поиска — общий формат для всех площадок.

Формат совпадает с ответом api.hh.ru (GET /vacancies), остальные площадки приводят свои
ответы к нему. Описаны только поля, которые нужны сервису. Остальные поля сохраняются
(extra="allow"), поэтому model_dump(by_alias=True) возвращает полный ответ — он пишется
в vacancies.raw.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SourceModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class IdName(SourceModel):
    id: str
    name: str


class Salary(SourceModel):
    from_: int | None = Field(default=None, alias="from")
    to: int | None = None
    currency: str | None = None
    gross: bool | None = None


class Employer(SourceModel):
    # У анонимных работодателей id нет
    id: str | None = None
    name: str
    alternate_url: str | None = None


class VacancyShort(SourceModel):
    """Вакансия из выдачи поиска. id у площадок, кроме hh.ru, — с префиксом (см. portals)."""

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
