"""Задачи arq: полная синхронизация подборок (раз в 2 часа и по запросу бота).

ctx: redis, session_factory, source (FullSearchSource).
"""

from datetime import UTC, datetime
from typing import Any

import structlog

from app.db.repositories import SubscriptionRepository
from app.services.catalog import SyncResult, sync_catalog
from app.workers.locks import release

logger = structlog.get_logger(__name__)

CATALOG_LOCK_TTL = 15 * 60
CATALOG_LOCK_WAIT = 120


def catalog_lock_key(subscription_id: int) -> str:
    return f"catalog:subscription:{subscription_id}"


async def _sync_one(ctx: dict[str, Any], subscription_id: int, *, wait: bool) -> SyncResult | None:
    lock = ctx["redis"].lock(
        catalog_lock_key(subscription_id),
        timeout=CATALOG_LOCK_TTL,
        blocking_timeout=CATALOG_LOCK_WAIT,
    )
    if not await lock.acquire(blocking=wait):
        return None
    try:
        async with ctx["session_factory"]() as session:
            try:
                result = await sync_catalog(
                    session, ctx["source"], subscription_id, datetime.now(UTC)
                )
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                logger.exception("catalog_sync_failed", subscription_id=subscription_id)
                return None
    finally:
        await release(lock)


async def sync_catalogs(ctx: dict[str, Any]) -> dict[str, int]:
    async with ctx["session_factory"]() as session:
        ids = [s.id for s in await SubscriptionRepository(session).list_catalog()]
    synced = 0
    for subscription_id in ids:
        if await _sync_one(ctx, subscription_id, wait=False):
            synced += 1
    return {"subscriptions": len(ids), "synced": synced}


async def sync_catalog_now(ctx: dict[str, Any], subscription_id: int) -> int | None:
    """Бот ставит после создания подписки-подборки или изменения её фильтров."""
    result = await _sync_one(ctx, subscription_id, wait=True)
    return result.kept if result else None
