import asyncio
from datetime import datetime
from types import TracebackType
from typing import Any, Self

import httpx
import structlog

from app.config import Settings
from app.ratelimit import Sleep
from app.sources.base import SearchFilters
from app.sources.hh.models import Area, ProfessionalRoleCategory, Vacancy, VacancySearchPage
from app.sources.http import HHAPIError, QueryItems, RetryingClient
from app.sources.models import IdName, VacancyShort
from app.sources.portals import Portal

logger = structlog.get_logger(__name__)

# hh.ru отдаёт не больше 2000 вакансий на один поисковый запрос (page * per_page)
MAX_SEARCH_DEPTH = 2000
MAX_PER_PAGE = 100


def build_search_params(
    filters: SearchFilters,
    date_from: datetime | None = None,
    page: int = 0,
    per_page: int = MAX_PER_PAGE,
) -> QueryItems:
    """Параметры GET /vacancies. Списочные фильтры передаются повторяющимися ключами."""
    params: QueryItems = [
        ("order_by", "publication_time"),
        ("page", str(page)),
        ("per_page", str(per_page)),
    ]
    if filters.text:
        params.append(("text", filters.text))
    params += [("professional_role", role) for role in filters.professional_roles]
    params += [("area", area) for area in filters.areas]
    params += [("experience", exp) for exp in filters.experience]
    params += [("work_format", fmt) for fmt in filters.work_format]
    if filters.salary is not None:
        params.append(("salary", str(filters.salary)))
    if filters.only_with_salary:
        params.append(("only_with_salary", "true"))
    if date_from is not None:
        params.append(("date_from", date_from.isoformat(timespec="seconds")))
    return params


class HHClient:
    def __init__(
        self,
        user_agent: str,
        app_token: str | None = None,
        base_url: str = "https://api.hh.ru",
        min_request_interval: float = 0.5,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        headers = {"HH-User-Agent": user_agent, "User-Agent": user_agent}
        if app_token:
            headers["Authorization"] = f"Bearer {app_token}"
        self._http = RetryingClient(
            base_url,
            headers,
            min_request_interval,
            max_retries=max_retries,
            backoff_base=backoff_base,
            timeout=timeout,
            transport=transport,
            sleep=sleep,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        token = settings.hh_app_token.get_secret_value() if settings.hh_app_token else None
        return cls(
            user_agent=settings.hh_user_agent,
            app_token=token,
            base_url=settings.hh_api_base_url,
            min_request_interval=settings.hh_min_request_interval,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def _get(self, path: str, params: QueryItems | None = None) -> Any:
        return (await self._http.get(path, params)).json()

    async def search_vacancies(
        self,
        filters: SearchFilters,
        date_from: datetime | None = None,
        page: int = 0,
        per_page: int = MAX_PER_PAGE,
    ) -> VacancySearchPage:
        data = await self._get(
            "/vacancies", build_search_params(filters, date_from, page, per_page)
        )
        return VacancySearchPage.model_validate(data)

    async def search_all(
        self, filters: SearchFilters, date_from: datetime | None = None
    ) -> list[VacancyShort]:
        """Все страницы выдачи (в пределах лимита глубины hh.ru)."""
        items: list[VacancyShort] = []
        page = 0
        while True:
            result = await self.search_vacancies(filters, date_from, page=page)
            items += result.items
            page += 1
            if page >= result.pages or (page + 1) * MAX_PER_PAGE > MAX_SEARCH_DEPTH:
                if result.found > MAX_SEARCH_DEPTH:
                    logger.warning("hh_search_truncated", found=result.found, got=len(items))
                return items

    async def get_vacancy(self, vacancy_id: str) -> Vacancy:
        return Vacancy.model_validate(await self._get(f"/vacancies/{vacancy_id}"))

    async def get_dictionaries(self) -> dict[str, list[IdName]]:
        """Справочники вида [{id, name}]. Разделы другой формы (например, currency) пропускаются."""
        data: dict[str, Any] = await self._get("/dictionaries")
        return {
            key: [IdName.model_validate(item) for item in value]
            for key, value in data.items()
            if isinstance(value, list)
            and all(isinstance(item, dict) and {"id", "name"} <= item.keys() for item in value)
        }

    async def get_professional_roles(self) -> list[ProfessionalRoleCategory]:
        data = await self._get("/professional_roles")
        return [ProfessionalRoleCategory.model_validate(c) for c in data["categories"]]

    async def get_areas(self) -> list[Area]:
        return [Area.model_validate(a) for a in await self._get("/areas")]


class HHApiSource:
    """VacancySource поверх официального API."""

    portal = Portal.HH
    name = "api"

    def __init__(self, client: HHClient) -> None:
        self._client = client

    async def search_new(self, filters: SearchFilters, date_from: datetime) -> list[VacancyShort]:
        return await self._client.search_all(filters, date_from)

    async def search_all(self, filters: SearchFilters) -> list[VacancyShort]:
        return await self._client.search_all(filters)


__all__ = ["HHAPIError", "HHApiSource", "HHClient", "build_search_params"]
