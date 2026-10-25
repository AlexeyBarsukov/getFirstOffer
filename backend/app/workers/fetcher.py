"""Задачи arq: периодический опрос подписок и внеочередная проверка одной подписки.

ctx (заполняется в app.workers.main.startup):
    redis            — ArqRedis: блокировки и постановка задачи отправки уведомлений
    session_factory  — async_sessionmaker
    source           — VacancySource
"""

from datetime import UTC, datetime
from typing import Any

import structlog

from app.db.repositories import SubscriptionRepository
from app.services.fetcher import CheckResult, check_subscription
from app.workers.locks import release
from app.workers.notifier import enqueue_send_now

logger = structlog.get_logger(__name__)

CYCLE_LOCK_KEY = "fetcher:cycle"
# Цикл не должен идти дольше нескольких минут; TTL — страховка, если воркер упал с блокировкой
CYCLE_LOCK_TTL = 10 * 60
SUBSCRIPTION_LOCK_TTL = 5 * 60
# Сколько внеочередная проверка ждёт, пока подписку проверяет плановый цикл
SUBSCRIPTION_LOCK_WAIT = 60


def subscription_lock_key(subscription_id: int) -> str:
    return f"fetcher:subscription:{subscription_id}"


async def _check_one(
    ctx: dict[str, Any], subscription_id: int, *, wait: bool
) -> CheckResult | None:
    lock = ctx["redis"].lock(
        subscription_lock_key(subscription_id),
        timeout=SUBSCRIPTION_LOCK_TTL,
        blocking_timeout=SUBSCRIPTION_LOCK_WAIT,
    )
    if not await lock.acquire(blocking=wait):
        logger.info("subscription_check_skipped_locked", subscription_id=subscription_id)
        return None
    try:
        async with ctx["session_factory"]() as session:
            try:
                result = await check_subscription(
                    session, ctx["source"], subscription_id, datetime.now(UTC)
                )
                await session.commit()
            except Exception:
                # Ошибка одной подписки (403, таймаут hh.ru, ...) не должна останавливать цикл.
                # last_checked_at не сдвигается, следующий цикл повторит то же окно поиска
                await session.rollback()
                logger.exception("subscription_check_failed", subscription_id=subscription_id)
                return None
    finally:
        await release(lock)
    if result is not None and result.new_events:
        # Не ждём конца цикла по остальным подпискам — отправляем сразу
        await enqueue_send_now(ctx)
    return result


async def fetch_new_vacancies(ctx: dict[str, Any]) -> dict[str, int]:
    """Cron раз в минуту: проверить все активные подписки."""
    cycle_lock = ctx["redis"].lock(CYCLE_LOCK_KEY, timeout=CYCLE_LOCK_TTL)
    if not await cycle_lock.acquire(blocking=False):
        logger.warning("fetch_cycle_skipped_previous_still_running")
        return {"checked": 0, "not_checked": 0, "new_events": 0}
    try:
        async with ctx["session_factory"]() as session:
            ids = [s.id for s in await SubscriptionRepository(session).list_active()]

        # not_checked: ошибка, подписка занята другой проверкой или поставлена на паузу
        checked = not_checked = new_events = 0
        for subscription_id in ids:
            result = await _check_one(ctx, subscription_id, wait=False)
            if result is None:
                not_checked += 1
            else:
                checked += 1
                new_events += result.new_events
        summary = {"checked": checked, "not_checked": not_checked, "new_events": new_events}
        logger.info("fetch_cycle_done", **summary)
        return summary
    finally:
        await release(cycle_lock)


async def check_subscription_now(ctx: dict[str, Any], subscription_id: int) -> int | None:
    """Внеочередная проверка — бот ставит её сразу после создания или изменения подписки."""
    result = await _check_one(ctx, subscription_id, wait=True)
    return result.new_events if result else None
