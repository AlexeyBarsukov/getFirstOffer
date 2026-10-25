"""Полная подборка: все вакансии по фильтрам подписки, чтобы разобрать их по одной.

Синхронизация собирает всю выдачу (без уведомлений), а пользователь отмечает каждую
вакансию: «откликнулся», «не подходит» или «позже».
"""

from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarkStatus, SourceName
from app.db.repositories import SubscriptionRepository, VacancyRepository
from app.db.repositories.catalog import CatalogCounts, CatalogItem, CatalogRepository
from app.services.filtering import find_excluded_word, is_excluded
from app.sources.base import FullSearchSource
from app.sources.employers import employer_key

logger = structlog.get_logger(__name__)

# Сколько кандидатов выбирать за раз, чтобы пропустить недавно скрытые компании
CANDIDATES_BATCH = 50


@dataclass(frozen=True)
class SyncResult:
    subscription_id: int
    found: int
    kept: int


@dataclass(frozen=True)
class CatalogCard:
    item: CatalogItem
    counts: CatalogCounts


async def sync_catalog(
    session: AsyncSession, source: FullSearchSource, subscription_id: int, now: datetime
) -> SyncResult | None:
    subscriptions = SubscriptionRepository(session)
    subscription = await subscriptions.get(subscription_id)
    if subscription is None or not subscription.is_active or not subscription.catalog:
        return None

    found = await source.search_all(subscription.search_filters)
    kept = [
        v
        for v in found
        if not is_excluded(v, subscription.exclude_words, subscription.exclude_employers)
    ]
    vacancies = VacancyRepository(session)
    await vacancies.upsert_many(kept, SourceName(source.name))
    await vacancies.touch_matches(subscription.id, [v.id for v in kept], now)
    subscription.catalog_synced_at = now
    if subscription.last_checked_at is None:
        # Всё, что есть сейчас, уже в подборке — уведомлять будем только о новых
        subscription.last_checked_at = now

    result = SyncResult(subscription_id=subscription.id, found=len(found), kept=len(kept))
    logger.info("catalog_synced", source=source.name, **result.__dict__)
    return result


async def next_card(session: AsyncSession, user_id: int) -> CatalogCard | None:
    """Следующая неразобранная вакансия подборки или None, если всё разобрано."""
    catalog = CatalogRepository(session)
    subscriptions = await SubscriptionRepository(session).list_for_user(user_id)
    # Компании и стоп-слова, добавленные после синхронизации, учитываем сразу
    hidden = {e for s in subscriptions for e in s.exclude_employers}
    words = {w for s in subscriptions if s.catalog for w in s.exclude_words}

    for item in await catalog.candidates(user_id, CANDIDATES_BATCH):
        vacancy = item.vacancy
        if vacancy.employer_id in hidden or employer_key(vacancy.employer_name) in hidden:
            continue
        if find_excluded_word(vacancy.name, words):
            continue
        return CatalogCard(item=item, counts=await catalog.counts(user_id))
    return None


async def mark(session: AsyncSession, user_id: int, vacancy_id: str, status: MarkStatus) -> None:
    await CatalogRepository(session).set_mark(user_id, vacancy_id, status)
