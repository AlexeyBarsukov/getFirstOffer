"""Задачи arq: полная синхронизация подборок (раз в 2 часа и по запросу бота).

Каждая площадка синхронизируется отдельно: ошибка одной не трогает подборку с других.

ctx: redis, session_factory, portals (dict[Portal, PortalSources]).
"""

from datetime import UTC, datetime
from typing import Any

import structlog

from app.db.repositories import SubscriptionRepository
from app.services.catalog import SyncResult, sync_catalog
from app.sources.portals import Portal
from app.sources.registry import PortalSources
from app.workers.locks import release

logger = structlog.get_logger(__name__)

CATALOG_LOCK_TTL = 15 * 60
CATALOG_LOCK_WAIT = 120


def catalog_lock_key(subscription_id: int, portal: Portal) -> str:
    return f"catalog:subscription:{subscription_id}:{portal}"


async def _sync_one(
    ctx: dict[str, Any], portal: Portal, subscription_id: int, *, wait: bool
) -> SyncResult | None:
    portals: dict[Portal, PortalSources] = ctx["portals"]
    lock = ctx["redis"].lock(
        catalog_lock_key(subscription_id, portal),
        timeout=CATALOG_LOCK_TTL,
        blocking_timeout=CATALOG_LOCK_WAIT,
    )
    if not await lock.acquire(blocking=wait):
        return None
    try:
        async with ctx["session_factory"]() as session:
            try:
                result = await sync_catalog(
                    session, portals[portal].search, subscription_id, datetime.now(UTC)
                )
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                logger.exception(
                    "catalog_sync_failed", subscription_id=subscription_id, portal=portal
                )
                return None
    finally:
        await release(lock)


async def sync_catalogs(ctx: dict[str, Any]) -> dict[str, int]:
    async with ctx["session_factory"]() as session:
        subscriptions = await SubscriptionRepository(session).list_catalog()
    synced = 0
    for subscription in subscriptions:
        for portal in ctx["portals"]:
            if subscription.uses_portal(portal) and await _sync_one(
                ctx, portal, subscription.id, wait=False
            ):
                synced += 1
    return {"subscriptions": len(subscriptions), "synced": synced}


async def sync_catalog_now(ctx: dict[str, Any], subscription_id: int) -> int | None:
    """Бот ставит после создания подписки-подборки или изменения её фильтров.

    Возвращает, сколько вакансий попало в подборку со всех площадок.
    """
    total: int | None = None
    for portal in ctx["portals"]:
        result = await _sync_one(ctx, portal, subscription_id, wait=True)
        if result is not None:
            total = (total or 0) + result.kept
    return total
