"""Задача arq: отправить все ожидающие уведомления.

Запускается сразу после того, как Fetcher нашёл новые вакансии, и раз в минуту — на всякий случай
(повторы после ошибок, пропущенный запуск).

ctx: redis, session_factory, notifiers (dict[ChannelType, Notifier]).
"""

from collections import Counter
from datetime import UTC, datetime
from typing import Any

import structlog

from app.db.repositories import EventRepository
from app.services.notifier import process_delivery
from app.workers.locks import release

logger = structlog.get_logger(__name__)

NOTIFIER_LOCK_KEY = "notifier:cycle"
NOTIFIER_LOCK_TTL = 10 * 60
BATCH_SIZE = 50
# Фиксированный job_id: пока задача в очереди или выполняется, повторная постановка игнорируется
SEND_NOW_JOB_ID = "send-notifications-now"


async def send_notifications(ctx: dict[str, Any]) -> dict[str, int]:
    lock = ctx["redis"].lock(NOTIFIER_LOCK_KEY, timeout=NOTIFIER_LOCK_TTL)
    if not await lock.acquire(blocking=False):
        # Уже идёт отправка — она подхватит и новые доставки
        return {}
    stats: Counter[str] = Counter()
    try:
        processed: set[tuple[int, int]] = set()
        while True:
            async with ctx["session_factory"]() as session:
                pending = await EventRepository(session).pending_deliveries(BATCH_SIZE)
                keys = [(d.event_id, d.channel_id) for d in pending]
            # Доставки, которые в этом запуске уже упали, повторим в следующем
            keys = [k for k in keys if k not in processed]
            if not keys:
                break
            for event_id, channel_id in keys:
                processed.add((event_id, channel_id))
                async with ctx["session_factory"]() as session:
                    status = await process_delivery(
                        session, ctx["notifiers"], event_id, channel_id, datetime.now(UTC)
                    )
                    await session.commit()
                stats[status.value if status else "not_configured"] += 1
        if stats:
            logger.info("notifications_done", **stats)
        return dict(stats)
    finally:
        await release(lock)


async def enqueue_send_now(ctx: dict[str, Any]) -> None:
    await ctx["redis"].enqueue_job("send_notifications", _job_id=SEND_NOW_JOB_ID)
