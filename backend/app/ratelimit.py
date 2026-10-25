import asyncio
import time
from collections.abc import Awaitable, Callable

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


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
