from typing import Any

import structlog
from redis.exceptions import LockError

logger = structlog.get_logger(__name__)


async def release(lock: Any) -> None:
    try:
        await lock.release()
    except LockError:
        # Блокировка истекла по TTL — её уже нет
        logger.warning("lock_expired", key=lock.name)
