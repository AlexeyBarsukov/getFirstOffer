"""HTTP-клиент для hh.ru с ограничением частоты и повторами (общий для API и RSS)."""

import asyncio

import httpx
import structlog

from app.ratelimit import RateLimiter, Sleep

logger = structlog.get_logger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Совместимо с типом query-параметров httpx
QueryItems = list[tuple[str, str | int | float | bool | None]]


class HHAPIError(Exception):
    def __init__(self, status_code: int, error_types: list[str], request_id: str | None) -> None:
        self.status_code = status_code
        self.error_types = error_types
        self.request_id = request_id
        super().__init__(
            f"hh.ru {status_code}: {', '.join(error_types) or 'unknown'} (request_id={request_id})"
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


class RetryingClient:
    """GET с паузой между запросами и повторами при 429 / 5xx / сетевых ошибках.

    Ответ с другой ошибкой (403, 404, ...) — сразу HHAPIError.
    """

    def __init__(
        self,
        base_url: str,
        headers: dict[str, str],
        min_request_interval: float,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=timeout, transport=transport
        )
        self._limiter = RateLimiter(min_request_interval, sleep=sleep)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, path: str, params: QueryItems | None = None) -> httpx.Response:
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
            return response
        raise AssertionError("unreachable")

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return float(retry_after)
        return self._backoff(attempt)

    def _backoff(self, attempt: int) -> float:
        return self._backoff_base * 2.0**attempt
