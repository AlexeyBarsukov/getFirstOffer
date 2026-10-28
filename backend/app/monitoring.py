"""Алерты администратору в Telegram.

- Воркер после каждого цикла пишет heartbeat и считает циклы, в которых не удалось
  проверить ни одной подписки (403 от hh.ru, сеть). Счёт — по каждой площадке отдельно.
  На N-м подряд — алерт, при восстановлении — ещё один.
- Бот раз в минуту смотрит heartbeat: если воркер давно не отрабатывал — алерт.
"""

from datetime import datetime, timedelta
from typing import Any, Protocol

import structlog
from aiogram import Bot

from app.sources.portals import PORTAL_TITLES, Portal

logger = structlog.get_logger(__name__)

HEARTBEAT_KEY = "monitor:fetcher_last_cycle_at"
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


def failed_cycles_key(portal: Portal) -> str:
    return f"monitor:fetcher_failed_cycles:{portal}"


def last_error_key(portal: Portal) -> str:
    return f"monitor:fetcher_last_error:{portal}"


def _decode(value: Any) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


async def record_last_error(redis: Any, portal: Portal, error: str) -> None:
    await redis.set(last_error_key(portal), error[:500])


async def record_cycle(
    redis: Any,
    alerter: Alerter,
    *,
    portal: Portal,
    checked: int,
    not_checked: int,
    now: datetime,
) -> None:
    await redis.set(HEARTBEAT_KEY, now.isoformat())
    title = PORTAL_TITLES[portal]
    if checked == 0 and not_checked > 0:
        failed = int(await redis.incr(failed_cycles_key(portal)))
        if failed == ALERT_AFTER_FAILED_CYCLES:
            error = _decode(await redis.get(last_error_key(portal))) or "неизвестно"
            await alerter.alert(
                f"⚠️ {title}: уже {failed} циклов подряд не удаётся проверить"
                f" ни одной подписки.\nПоследняя ошибка: {error}"
            )
        return
    previous = int(_decode(await redis.getset(failed_cycles_key(portal), 0)) or 0)
    if previous >= ALERT_AFTER_FAILED_CYCLES:
        await alerter.alert(f"✅ {title}: проверка подписок снова работает.")


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
