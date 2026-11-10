"""Задача arq: перепроверить уже найденные вакансии (раз в 30 минут).

ctx: redis, session_factory, portals (dict[Portal, PortalSources]) — детали вакансии
берутся у её площадки.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.db.repositories import VacancyRepository
from app.services.tracker import track_vacancy
from app.sources.http import SourceThrottledError
from app.sources.portals import Portal, portal_of
from app.sources.registry import PortalSources
from app.workers.locks import release
from app.workers.notifier import enqueue_send_now

logger = structlog.get_logger(__name__)

TRACKER_LOCK_KEY = "tracker:cycle"
TRACKER_LOCK_TTL = 25 * 60
# Следим за вакансиями, найденными за последние 30 дней
TRACK_PERIOD = timedelta(days=30)
# Одну вакансию проверяем не чаще: зарплату и статус меняют редко, а каждый запрос к hh.ru
# приближает антибот
RECHECK_AFTER = timedelta(hours=3)
# За один запуск — давно не проверенные первыми; страницы hh.ru — раз в 3 секунды
TRACK_BATCH = 200


async def track_vacancies(ctx: dict[str, Any]) -> dict[str, int]:
    lock = ctx["redis"].lock(TRACKER_LOCK_KEY, timeout=TRACKER_LOCK_TTL)
    if not await lock.acquire(blocking=False):
        logger.warning("tracker_skipped_previous_still_running")
        return {}
    portals: dict[Portal, PortalSources] = ctx["portals"]
    try:
        now = datetime.now(UTC)
        async with ctx["session_factory"]() as session:
            vacancies = await VacancyRepository(session).list_for_tracking(
                now - TRACK_PERIOD, TRACK_BATCH, checked_before=now - RECHECK_AFTER
            )
            ids = [v.id for v in vacancies]

        checked = failed = events = 0
        # Площадки, которые в этом проходе ответили заглушкой антибота: до следующего запуска
        # к ним не ходим
        throttled: set[Portal] = set()
        for vacancy_id in ids:
            portal_name = portal_of(vacancy_id)
            portal = portals.get(portal_name)
            if portal is None or portal.details is None or portal_name in throttled:
                # Площадка выключена, не умеет отдавать детали или ограничила запросы
                continue
            async with ctx["session_factory"]() as session:
                try:
                    events += await track_vacancy(
                        session, portal.details, vacancy_id, datetime.now(UTC)
                    )
                    await session.commit()
                    checked += 1
                except SourceThrottledError as exc:
                    await session.rollback()
                    logger.warning(
                        "tracker_throttled", portal=portal_name, checked=checked, error=str(exc)
                    )
                    throttled.add(portal_name)
                except Exception:
                    # Не удалось проверить (403 на странице, таймаут) — попробуем в следующий раз
                    await session.rollback()
                    logger.exception("vacancy_track_failed", vacancy_id=vacancy_id)
                    failed += 1

        summary = {"checked": checked, "failed": failed, "events": events}
        if throttled:
            summary["throttled"] = len(throttled)
        logger.info("tracker_done", **summary)
        if events:
            await enqueue_send_now(ctx)
        return summary
    finally:
        await release(lock)
