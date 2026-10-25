import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from types import TracebackType
from typing import Any, Self

import httpx
import structlog

from app.config import Settings
from app.sources.base import SearchFilters
from app.sources.hh_models import (
    Area,
    IdName,
    ProfessionalRoleCategory,
    Vacancy,
    VacancySearchPage,
    VacancyShort,
)

logger = structlog.get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
# Совместимо с типом query-параметров httpx
QueryItems = list[tuple[str, str | int | float | bool | None]]

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# hh.ru отдаёт не больше 2000 вакансий на один поисковый запрос (page * per_page)
MAX_SEARCH_DEPTH = 2000
MAX_PER_PAGE = 100


class HHAPIError(Exception):
    def __init__(self, status_code: int, error_types: list[str], request_id: str | None) -> None:
        self.status_code = status_code
        self.error_types = error_types
        self.request_id = request_id
        super().__init__(
            f"hh.ru API {status_code}: {', '.join(error_types) or 'unknown'} "
            f"(request_id={request_id})"
        )

    @classmethod
    def from_response(cls, response: httpx.Response) -> "HHAPIError":
        error_types: list[str] = []
        request_id: str | None = None
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            error_types = [
                str(e.get("type")) for e in body.get("errors", []) if isinstance(e, dict)
            ]
            request_id = body.get("request_id")
        return cls(response.status_code, error_types, request_id)


class RateLimiter:
    """Гарантирует минимальный интервал между запросами."""

    def __init__(
        self, min_interval: float, sleep: Sleep = asyncio.sleep, clock: Clock = time.monotonic
    ) -> None:
        self._min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._lock = asyncio.Lock()
        self._last_call: float | None = None

    async def acquire(self) -> None:
        async with self._lock:
            if self._last_call is not None:
                wait = self._min_interval - (self._clock() - self._last_call)
                if wait > 0:
                    await self._sleep(wait)
            self._last_call = self._clock()


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
        self._http = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=timeout, transport=transport
        )
        self._limiter = RateLimiter(min_request_interval, sleep=sleep)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep

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
        for attempt in range(self._max_retries + 1):
            is_last = attempt == self._max_retries
            await self._limiter.acquire()
            try:
                response = await self._http.get(path, params=params)
            except httpx.TransportError as exc:
                if is_last:
                    raise
                delay = self._backoff(attempt)
                logger.warning("hh_request_failed", path=path, error=repr(exc), retry_in=delay)
                await self._sleep(delay)
                continue

            if response.status_code in RETRY_STATUSES and not is_last:
                delay = self._retry_delay(response, attempt)
                logger.warning(
                    "hh_request_retry", path=path, status=response.status_code, retry_in=delay
                )
                await self._sleep(delay)
                continue
            if response.is_error:
                raise HHAPIError.from_response(response)
            return response.json()
        raise AssertionError("unreachable")

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return float(retry_after)
        return self._backoff(attempt)

    def _backoff(self, attempt: int) -> float:
        return self._backoff_base * 2.0**attempt

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

    name = "api"

    def __init__(self, client: HHClient) -> None:
        self._client = client

    async def search_new(self, filters: SearchFilters, date_from: datetime) -> list[VacancyShort]:
        return await self._client.search_all(filters, date_from)
