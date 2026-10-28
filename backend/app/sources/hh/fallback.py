import time
from collections.abc import Awaitable, Callable

import structlog

from app.ratelimit import Clock
from app.sources.http import HHAPIError

logger = structlog.get_logger(__name__)

# Как часто снова пробовать API после 403
API_RECHECK_INTERVAL = 30 * 60


class ApiFallback:
    """API hh.ru, пока он отвечает; при 403 — запасной путь, API перепроверяется раз в 30 минут.

    Другие ошибки API (500, сеть) не маскируются — пусть их обработает вызывающий код.
    """

    def __init__(
        self,
        label: str,
        recheck_interval: float = API_RECHECK_INTERVAL,
        clock: Clock = time.monotonic,
    ) -> None:
        self._label = label
        self._recheck_interval = recheck_interval
        self._clock = clock
        self._blocked_until: float | None = None

    async def call[T](
        self,
        api_call: Callable[[], Awaitable[T]],
        fallback_call: Callable[[], Awaitable[T]],
    ) -> tuple[T, bool]:
        """Результат и признак, что он получен через API."""
        blocked = self._blocked_until is not None
        if not blocked or self._clock() >= (self._blocked_until or 0):
            try:
                result = await api_call()
            except HHAPIError as exc:
                if exc.status_code != 403:
                    raise
                if not blocked:
                    logger.warning(
                        "hh_api_forbidden_using_fallback", what=self._label, error=str(exc)
                    )
                self._blocked_until = self._clock() + self._recheck_interval
            else:
                if blocked:
                    logger.info("hh_api_available_again", what=self._label)
                self._blocked_until = None
                return result, True
        return await fallback_call(), False
