"""Задача arq: перепроверить уже найденные вакансии (раз в 30 минут).

ctx: redis, session_factory, details_source (VacancyDetailsSource).
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.db.repositories import VacancyRepository
from app.services.tracker import track_vacancy
from app.workers.locks import release
from app.workers.notifier import enqueue_send_now

logger = structlog.get_logger(__name__)

TRACKER_LOCK_KEY = "tracker:cycle"
TRACKER_LOCK_TTL = 25 * 60
# Следим за вакансиями, найденными за последние 30 дней
TRACK_PERIOD = timedelta(days=30)
# За один запуск — давно не проверенные первыми; ~1 запрос в секунду к hh.ru
TRACK_BATCH = 200


async def track_vacancies(ctx: dict[str, Any]) -> dict[str, int]:
    lock = ctx["redis"].lock(TRACKER_LOCK_KEY, timeout=TRACKER_LOCK_TTL)
    if not await lock.acquire(blocking=False):
        logger.warning("tracker_skipped_previous_still_running")
        return {}
    try:
        now = datetime.now(UTC)
        async with ctx["session_factory"]() as session:
            vacancies = await VacancyRepository(session).list_for_tracking(
                now - TRACK_PERIOD, TRACK_BATCH
            )
            ids = [v.id for v in vacancies]

        checked = failed = events = 0
        for vacancy_id in ids:
            async with ctx["session_factory"]() as session:
                try:
                    events += await track_vacancy(
                        session, ctx["details_source"], vacancy_id, datetime.now(UTC)
                    )
                    await session.commit()
                    checked += 1
                except Exception:
                    # Не удалось проверить (403 на странице, таймаут) — попробуем в следующий раз
                    await session.rollback()
                    logger.exception("vacancy_track_failed", vacancy_id=vacancy_id)
                    failed += 1

        summary = {"checked": checked, "failed": failed, "events": events}
        logger.info("tracker_done", **summary)
        if events:
            await enqueue_send_now(ctx)
        return summary
    finally:
        await release(lock)
