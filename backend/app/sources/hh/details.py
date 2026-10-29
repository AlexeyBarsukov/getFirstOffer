"""Текущее состояние вакансии hh.ru — для отслеживания изменений.

Источники: API (GET /vacancies/{id}) и страница вакансии на сайте (встроенный JSON
HH-Lux-InitialState), а также AutoDetailsSource — API, при 403 страница.
"""

import json
import re
import time
from html import unescape

import httpx

from app.ratelimit import Clock
from app.sources.details import VacancyDetails, VacancyDetailsSource, salary_dict
from app.sources.hh.api import HHClient
from app.sources.hh.fallback import API_RECHECK_INTERVAL, ApiFallback
from app.sources.http import HHAPIError, RetryingClient

_INITIAL_STATE = re.compile(r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', re.S)


def parse_vacancy_page(html: str) -> VacancyDetails:
    match = _INITIAL_STATE.search(html)
    if not match:
        raise ValueError("на странице нет HH-Lux-InitialState")
    vacancy = json.loads(unescape(match.group(1)))["vacancyView"]["vacancyFull"]["vacancy"]
    # from/to — базовая ставка; perModeFrom/perModeTo — сумма за период (например, за вахту)
    compensation = vacancy.get("compensation") or {}
    company_id = (vacancy.get("company") or {}).get("id")
    return VacancyDetails(
        id=str(vacancy["vacancyId"]),
        name=vacancy["name"],
        archived=bool((vacancy.get("status") or {}).get("archived")),
        employer_id=str(company_id) if company_id is not None else None,
        salary=salary_dict(
            compensation.get("from"),
            compensation.get("to"),
            compensation.get("currencyCode"),
            compensation.get("gross"),
        ),
        experience=vacancy.get("workExperience"),
        work_format=list(vacancy.get("workFormats") or []),
        area=(vacancy.get("area") or {}).get("name"),
    )


class ApiDetailsSource:
    name = "api"

    def __init__(self, client: HHClient) -> None:
        self._client = client

    async def get_details(self, vacancy_id: str, url: str | None = None) -> VacancyDetails | None:
        try:
            vacancy = await self._client.get_vacancy(vacancy_id)
        except HHAPIError as exc:
            if exc.status_code == 404:
                return None
            raise
        salary = vacancy.salary
        return VacancyDetails(
            id=vacancy.id,
            name=vacancy.name,
            archived=vacancy.archived,
            employer_id=vacancy.employer.id,
            salary=salary_dict(salary.from_, salary.to, salary.currency, salary.gross)
            if salary
            else None,
            experience=vacancy.experience.id if vacancy.experience else None,
            work_format=[f.id for f in vacancy.work_format],
            area=vacancy.area.name,
        )


class PageDetailsSource:
    """Страница hh.ru/vacancy/{id}. 403 считается временной ошибкой (не «удалена»)."""

    name = "page"

    def __init__(
        self,
        user_agent: str,
        base_url: str = "https://hh.ru",
        min_request_interval: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = RetryingClient(
            base_url,
            {"User-Agent": user_agent},
            min_request_interval,
            max_retries=2,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_details(self, vacancy_id: str, url: str | None = None) -> VacancyDetails | None:
        try:
            response = await self._http.get(f"/vacancy/{vacancy_id}")
        except HHAPIError as exc:
            if exc.status_code == 404:
                return None
            raise
        return parse_vacancy_page(response.text)


class AutoDetailsSource:
    """API, а при 403 — страница вакансии. name — источник последнего ответа."""

    def __init__(
        self,
        api: VacancyDetailsSource,
        page: VacancyDetailsSource,
        recheck_interval: float = API_RECHECK_INTERVAL,
        clock: Clock = time.monotonic,
    ) -> None:
        self._api = api
        self._page = page
        self._fallback = ApiFallback("vacancy details", recheck_interval, clock)
        self.name = api.name

    async def get_details(self, vacancy_id: str, url: str | None = None) -> VacancyDetails | None:
        result, via_api = await self._fallback.call(
            lambda: self._api.get_details(vacancy_id),
            lambda: self._page.get_details(vacancy_id),
        )
        self.name = self._api.name if via_api else self._page.name
        return result
