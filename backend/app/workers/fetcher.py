"""Задачи arq: периодический опрос подписок и внеочередная проверка одной подписки.

У каждой площадки своя cron-задача (fetch_job) со своей частотой и блокировками: если одна
площадка недоступна, остальные продолжают работать.

ctx (заполняется в app.workers.main.startup):
    redis            — ArqRedis: блокировки и постановка задачи отправки уведомлений
    session_factory  — async_sessionmaker
    portals          — dict[Portal, PortalSources]
"""

from datetime import UTC, datetime
from typing import Any

import structlog
from arq.typing import WorkerCoroutine

from app.db.repositories import SubscriptionRepository
from app.monitoring import record_cycle, record_last_error
from app.services.fetcher import CheckResult, check_subscription
from app.sources.portals import Portal
from app.sources.registry import PortalSources
from app.workers.locks import release
from app.workers.notifier import enqueue_send_now

logger = structlog.get_logger(__name__)

# Цикл не должен идти дольше нескольких минут; TTL — страховка, если воркер упал с блокировкой
CYCLE_LOCK_TTL = 10 * 60
SUBSCRIPTION_LOCK_TTL = 5 * 60
# Сколько внеочередная проверка ждёт, пока подписку проверяет плановый цикл
SUBSCRIPTION_LOCK_WAIT = 60


def cycle_lock_key(portal: Portal) -> str:
    return f"fetcher:cycle:{portal}"


def subscription_lock_key(subscription_id: int, portal: Portal) -> str:
    return f"fetcher:subscription:{subscription_id}:{portal}"


def _portals(ctx: dict[str, Any]) -> dict[Portal, PortalSources]:
    portals: dict[Portal, PortalSources] = ctx["portals"]
    return portals


async def _check_one(
    ctx: dict[str, Any], portal: Portal, subscription_id: int, *, wait: bool
) -> CheckResult | None:
    lock = ctx["redis"].lock(
        subscription_lock_key(subscription_id, portal),
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
                    session, _portals(ctx)[portal].search, subscription_id, datetime.now(UTC)
                )
                await session.commit()
            except Exception as exc:
                # Ошибка одной подписки (403, таймаут площадки, ...) не должна останавливать цикл.
                # Окно поиска не сдвигается, следующий цикл повторит его
                await session.rollback()
                logger.exception(
                    "subscription_check_failed", subscription_id=subscription_id, portal=portal
                )
                await record_last_error(ctx["redis"], portal, repr(exc))
                return None
    finally:
        await release(lock)
    if result is not None and result.new_events:
        # Не ждём конца цикла по остальным подпискам — отправляем сразу
        await enqueue_send_now(ctx)
    return result


async def fetch_new_vacancies(ctx: dict[str, Any], portal: Portal) -> dict[str, int]:
    """Проверить все активные подписки на одной площадке."""
    cycle_lock = ctx["redis"].lock(cycle_lock_key(portal), timeout=CYCLE_LOCK_TTL)
    if not await cycle_lock.acquire(blocking=False):
        logger.warning("fetch_cycle_skipped_previous_still_running", portal=portal)
        return {"checked": 0, "not_checked": 0, "new_events": 0}
    try:
        async with ctx["session_factory"]() as session:
            ids = [
                s.id
                for s in await SubscriptionRepository(session).list_active()
                if s.uses_portal(portal)
            ]

        # not_checked: ошибка, подписка занята другой проверкой или поставлена на паузу
        checked = not_checked = new_events = 0
        for subscription_id in ids:
            result = await _check_one(ctx, portal, subscription_id, wait=False)
            if result is None:
                not_checked += 1
            else:
                checked += 1
                new_events += result.new_events
        summary = {"checked": checked, "not_checked": not_checked, "new_events": new_events}
        logger.info("fetch_cycle_done", portal=portal, **summary)
        await record_cycle(
            ctx["redis"],
            ctx["alerter"],
            portal=portal,
            checked=checked,
            not_checked=not_checked,
            now=datetime.now(UTC),
        )
        return summary
    finally:
        await release(cycle_lock)


def fetch_job(portal: Portal) -> WorkerCoroutine:
    """Cron-задача опроса одной площадки (у arq у каждой задачи своё имя)."""

    async def job(ctx: dict[Any, Any], *_: Any, **__: Any) -> dict[str, int]:
        return await fetch_new_vacancies(ctx, portal)

    job.__qualname__ = job.__name__ = f"fetch_new_vacancies_{portal}"
    return job


async def check_subscription_now(ctx: dict[str, Any], subscription_id: int) -> int | None:
    """Внеочередная проверка на всех площадках — бот ставит её сразу после создания
    или изменения подписки. None — ни одна площадка не проверила подписку."""
    total: int | None = None
    for portal in _portals(ctx):
        result = await _check_one(ctx, portal, subscription_id, wait=True)
        if result is not None:
            total = (total or 0) + result.new_events
    return total
