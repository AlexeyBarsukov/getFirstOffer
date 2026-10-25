"""Поиск новых вакансий по подписке.

Одна проверка: запрос к источнику → локальные фильтры → сохранение вакансий →
совпадения с подпиской → события «новая вакансия» → доставки во все активные каналы.
Транзакцией управляет вызывающий код (воркер): commit после успешной проверки.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SourceName
from app.db.repositories import (
    EventRepository,
    SubscriptionRepository,
    UserRepository,
    VacancyRepository,
)
from app.services.filtering import is_excluded
from app.sources.base import VacancySource

logger = structlog.get_logger(__name__)

# Запас по времени: вакансия может попасть в поисковый индекс позже своего published_at
SEARCH_OVERLAP = timedelta(minutes=5)
# Первая проверка подписки (или после смены фильтров) — вакансии за последние сутки
BACKFILL_PERIOD = timedelta(hours=24)


@dataclass(frozen=True)
class CheckResult:
    subscription_id: int
    is_backfill: bool
    found: int
    excluded: int
    new_matches: int
    new_events: int


def search_window_start(last_checked_at: datetime | None, now: datetime) -> tuple[datetime, bool]:
    """С какого момента искать и является ли проверка подгрузкой за 24 часа."""
    if last_checked_at is None:
        return now - BACKFILL_PERIOD, True
    return last_checked_at - SEARCH_OVERLAP, False


async def check_subscription(
    session: AsyncSession, source: VacancySource, subscription_id: int, now: datetime
) -> CheckResult | None:
    """Проверяет одну подписку. now — момент начала проверки (до запроса к источнику).

    Возвращает None, если подписка удалена или на паузе.
    """
    subscriptions = SubscriptionRepository(session)
    subscription = await subscriptions.get(subscription_id)
    if subscription is None or not subscription.is_active:
        return None

    if subscription.catalog and subscription.last_checked_at is None:
        # Подборку наполняет sync_catalog — всю текущую выдачу присылать уведомлениями не нужно.
        # Дальше уведомления придут только о новых вакансиях
        await subscriptions.mark_checked(subscription.id, now)
        return CheckResult(subscription.id, True, 0, 0, 0, 0)

    date_from, is_backfill = search_window_start(subscription.last_checked_at, now)
    found = await source.search_new(subscription.search_filters, date_from)
    kept = [
        v
        for v in found
        if not is_excluded(v, subscription.exclude_words, subscription.exclude_employers)
    ]

    vacancies = VacancyRepository(session)
    await vacancies.upsert_many(kept, SourceName(source.name))
    new_ids = await vacancies.add_matches(
        subscription.id, [v.id for v in kept], is_backfill=is_backfill, seen_at=now
    )

    events_repo = EventRepository(session)
    events = await events_repo.add_new_vacancy_events(subscription.user_id, new_ids)
    channel_ids = [
        c.id for c in await UserRepository(session).active_channels(subscription.user_id)
    ]
    for event in events:
        await events_repo.add_deliveries(event.id, channel_ids)

    await subscriptions.mark_checked(subscription.id, now)

    if not is_backfill:
        by_id = {v.id: v for v in kept}
        for event in events:
            vacancy = by_id[event.vacancy_id]
            # Главная метрика сервиса: через сколько после публикации мы узнали о вакансии
            logger.info(
                "vacancy_found",
                vacancy_id=vacancy.id,
                subscription_id=subscription.id,
                source=source.name,
                latency_s=round((now - vacancy.published_at).total_seconds()),
            )

    result = CheckResult(
        subscription_id=subscription.id,
        is_backfill=is_backfill,
        found=len(found),
        excluded=len(found) - len(kept),
        new_matches=len(new_ids),
        new_events=len(events),
    )
    logger.info("subscription_checked", **result.__dict__)
    return result
