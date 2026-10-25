"""Алерты администратору в Telegram.

- Воркер после каждого цикла пишет heartbeat и считает циклы, в которых не удалось
  проверить ни одной подписки (403 от hh.ru, сеть). На N-м подряд — алерт,
  при восстановлении — ещё один.
- Бот раз в минуту смотрит heartbeat: если воркер давно не отрабатывал — алерт.
"""

from datetime import datetime, timedelta
from typing import Any, Protocol

import structlog
from aiogram import Bot

logger = structlog.get_logger(__name__)

HEARTBEAT_KEY = "monitor:fetcher_last_cycle_at"
FAILED_CYCLES_KEY = "monitor:fetcher_failed_cycles"
LAST_ERROR_KEY = "monitor:fetcher_last_error"
STALE_ALERTED_KEY = "monitor:fetcher_stale_alerted"

ALERT_AFTER_FAILED_CYCLES = 5
STALE_AFTER = timedelta(minutes=5)


class Alerter(Protocol):
    async def alert(self, text: str) -> None: ...


class TelegramAlerter:
    def __init__(self, bot: Bot | None, chat_id: int | None) -> None:
        self._bot = bot
        self._chat_id = chat_id

    async def alert(self, text: str) -> None:
        logger.warning("alert", text=text)
        if self._bot is None or self._chat_id is None:
            return
        try:
            await self._bot.send_message(self._chat_id, text)
        except Exception:
            # Алерт не должен ронять воркер или бота
            logger.exception("alert_send_failed")


def _decode(value: Any) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


async def record_last_error(redis: Any, error: str) -> None:
    await redis.set(LAST_ERROR_KEY, error[:500])


async def record_cycle(
    redis: Any, alerter: Alerter, *, checked: int, not_checked: int, now: datetime
) -> None:
    await redis.set(HEARTBEAT_KEY, now.isoformat())
    if checked == 0 and not_checked > 0:
        failed = int(await redis.incr(FAILED_CYCLES_KEY))
        if failed == ALERT_AFTER_FAILED_CYCLES:
            error = _decode(await redis.get(LAST_ERROR_KEY)) or "неизвестно"
            await alerter.alert(
                f"⚠️ Уже {failed} циклов подряд не удаётся проверить ни одной подписки.\n"
                f"Последняя ошибка: {error}"
            )
        return
    previous = int(_decode(await redis.getset(FAILED_CYCLES_KEY, 0)) or 0)
    if previous >= ALERT_AFTER_FAILED_CYCLES:
        await alerter.alert("✅ Проверка подписок снова работает.")


async def check_heartbeat(redis: Any, alerter: Alerter, now: datetime) -> None:
    raw = _decode(await redis.get(HEARTBEAT_KEY))
    stale = raw is None or now - datetime.fromisoformat(raw) > STALE_AFTER
    alerted = await redis.get(STALE_ALERTED_KEY) is not None
    if stale and not alerted:
        await alerter.alert(
            "🛑 Воркер не проверял подписки больше 5 минут — похоже, он упал или завис."
        )
        await redis.set(STALE_ALERTED_KEY, 1)
    elif not stale and alerted:
        await alerter.alert("✅ Воркер снова работает.")
        await redis.delete(STALE_ALERTED_KEY)
