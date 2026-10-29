"""Одна вакансия на нескольких площадках: одно уведомление, одна карточка, общая отметка."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ChannelType,
    MarkStatus,
    Subscription,
    Vacancy,
    VacancyEvent,
    VacancyStatus,
)
from app.db.repositories import SubscriptionRepository, UserRepository, VacancyRepository
from app.db.repositories.catalog import CatalogRepository
from app.services.catalog import mark, next_card, sync_catalog
from app.services.fetcher import check_subscription
from app.sources.base import SearchFilters
from app.sources.portals import Portal
from tests.factories import FakeSource, make_vacancy

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


async def make_subscription(session: AsyncSession, *, catalog: bool = False) -> Subscription:
    user, _ = await UserRepository(session).get_or_create_by_channel(ChannelType.TELEGRAM, "1")
    return await SubscriptionRepository(session).create(
        user.id, "react", SearchFilters(text="react"), catalog=catalog
    )


async def events_count(session: AsyncSession) -> int:
    return (await session.scalar(select(func.count()).select_from(VacancyEvent))) or 0


async def test_duplicate_from_other_portal_is_not_notified_again(session: AsyncSession) -> None:
    subscription = await make_subscription(session)
    hh = FakeSource(
        {"react": [make_vacancy("1", "Frontend-разработчик (React)", published_at=NOW)]}
    )
    habr = FakeSource(
        {"react": [make_vacancy("habr-7", "Middle Frontend разработчик React", published_at=NOW)]},
        portal=Portal.HABR,
    )
    await check_subscription(session, hh, subscription.id, NOW)

    result = await check_subscription(session, habr, subscription.id, NOW + timedelta(minutes=2))

    assert result is not None and (result.new_events, result.duplicates) == (0, 1)
    assert await events_count(session) == 1
    duplicate = await session.get_one(Vacancy, "habr-7")
    assert duplicate.duplicate_of == "1"


async def test_same_portal_and_old_vacancies_are_not_duplicates(session: AsyncSession) -> None:
    subscription = await make_subscription(session)
    hh = FakeSource(
        {
            "react": [
                make_vacancy("1", "Frontend", published_at=NOW),
                # Та же должность в другом городе на той же площадке — отдельная вакансия
                make_vacancy("2", "Frontend", published_at=NOW),
            ]
        }
    )
    habr = FakeSource(
        {"react": [make_vacancy("habr-7", "Frontend", published_at=NOW + timedelta(days=30))]},
        portal=Portal.HABR,
    )
    await check_subscription(session, hh, subscription.id, NOW)
    # Через месяц на hh.ru вакансия уже в архиве — на Хабре это новый найм
    for vacancy_id in ("1", "2"):
        await VacancyRepository(session).mark_checked(vacancy_id, NOW, VacancyStatus.ARCHIVED)
    await check_subscription(session, habr, subscription.id, NOW + timedelta(days=30))

    assert await events_count(session) == 3
    links = await session.scalars(select(Vacancy.duplicate_of))
    assert set(links) == {None}


async def test_open_vacancy_matches_late_duplicate(session: AsyncSession) -> None:
    subscription = await make_subscription(session)
    hh = FakeSource({"react": [make_vacancy("1", "Frontend (AI)", published_at=NOW)]})
    # Как Bell Integrator: на hh.ru с 30 сентября, на Хабре — с 20 октября
    habr = FakeSource(
        {"react": [make_vacancy("habr-7", "Frontend (AI)", published_at=NOW + timedelta(days=20))]},
        portal=Portal.HABR,
    )
    await check_subscription(session, hh, subscription.id, NOW)
    await check_subscription(session, habr, subscription.id, NOW + timedelta(days=20))

    assert (await session.get_one(Vacancy, "habr-7")).duplicate_of == "1"


async def test_catalog_shows_group_once_and_marks_it_together(session: AsyncSession) -> None:
    subscription = await make_subscription(session, catalog=True)
    user_id = subscription.user_id
    hh = FakeSource(
        {
            "react": [
                make_vacancy("1", "Frontend-разработчик", published_at=NOW),
                make_vacancy("2", "Vue-разработчик", published_at=NOW - timedelta(days=1)),
            ]
        }
    )
    habr = FakeSource(
        {"react": [make_vacancy("habr-7", "Frontend разработчик", published_at=NOW)]},
        portal=Portal.HABR,
    )
    await sync_catalog(session, hh, subscription.id, NOW)
    await sync_catalog(session, habr, subscription.id, NOW)

    counts = await CatalogRepository(session).counts(user_id)
    assert counts.total == 2  # «Frontend» с двух площадок — одна вакансия
    card = await next_card(session, user_id)
    assert card is not None and card.item.vacancy.id == "1"
    assert [d.id for d in card.duplicates] == ["habr-7"]

    await mark(session, user_id, "1", MarkStatus.APPLIED)

    card = await next_card(session, user_id)
    assert card is not None and card.item.vacancy.id == "2"
    assert card.duplicates == []
    assert (card.counts.applied, card.counts.remaining) == (1, 1)
