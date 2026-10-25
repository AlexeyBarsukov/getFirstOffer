from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ChannelType,
    Delivery,
    SourceName,
    SubscriptionMatch,
    Vacancy,
    VacancyEvent,
)
from app.db.repositories import SubscriptionRepository, UserRepository
from app.services.fetcher import (
    BACKFILL_PERIOD,
    SEARCH_OVERLAP,
    check_subscription,
    search_window_start,
)
from app.sources.base import SearchFilters
from tests.factories import FakeSource, make_vacancy

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


async def count(session: AsyncSession, model: type) -> int:
    return (await session.scalar(select(func.count()).select_from(model))) or 0


async def setup_user(session: AsyncSession, chat_id: str = "1001") -> tuple[int, int]:
    user, channel = await UserRepository(session).get_or_create_by_channel(
        ChannelType.TELEGRAM, chat_id
    )
    return user.id, channel.id


def test_search_window() -> None:
    assert search_window_start(None, NOW) == (NOW - BACKFILL_PERIOD, True)
    last = NOW - timedelta(minutes=1)
    assert search_window_start(last, NOW) == (last - SEARCH_OVERLAP, False)


async def test_first_check_is_backfill_then_incremental(session: AsyncSession) -> None:
    user_id, channel_id = await setup_user(session)
    subscription = await SubscriptionRepository(session).create(
        user_id, "Frontend", SearchFilters(text="frontend")
    )
    old = make_vacancy("1", published_at=NOW - timedelta(hours=3))
    source = FakeSource({"frontend": [old]})

    first = await check_subscription(session, source, subscription.id, NOW)

    assert first is not None
    assert (first.is_backfill, first.new_matches, first.new_events) == (True, 1, 1)
    assert source.calls[0][1] == NOW - BACKFILL_PERIOD
    match = await session.get_one(SubscriptionMatch, (subscription.id, "1"))
    assert match.is_backfill is True
    delivery = (await session.scalars(select(Delivery))).one()
    assert delivery.channel_id == channel_id

    # Следующая минута: та же вакансия + новая
    fresh = make_vacancy("2", published_at=NOW + timedelta(seconds=30))
    source.responses["frontend"] = [fresh, old]
    later = NOW + timedelta(minutes=1)

    second = await check_subscription(session, source, subscription.id, later)

    assert second is not None
    assert (second.is_backfill, second.found, second.new_matches) == (False, 2, 1)
    assert source.calls[1][1] == NOW - SEARCH_OVERLAP
    assert await count(session, VacancyEvent) == 2
    assert subscription.last_checked_at == later


async def test_local_filters_drop_vacancies(session: AsyncSession) -> None:
    user_id, _ = await setup_user(session)
    subscription = await SubscriptionRepository(session).create(
        user_id,
        "Frontend",
        SearchFilters(text="frontend"),
        exclude_words=["senior"],
        exclude_employers=["666"],
    )
    source = FakeSource(
        {
            "frontend": [
                make_vacancy("1", "Senior Frontend", published_at=NOW),
                make_vacancy("2", "Junior Frontend", published_at=NOW, employer_id="666"),
                make_vacancy("3", "Junior Frontend", published_at=NOW),
            ]
        }
    )

    result = await check_subscription(session, source, subscription.id, NOW)

    assert result is not None
    assert (result.found, result.excluded, result.new_events) == (3, 2, 1)
    event = (await session.scalars(select(VacancyEvent))).one()
    assert event.vacancy_id == "3"


async def test_same_vacancy_in_two_subscriptions_gives_one_event(session: AsyncSession) -> None:
    user_id, _ = await setup_user(session)
    subscriptions = SubscriptionRepository(session)
    frontend = await subscriptions.create(user_id, "Frontend", SearchFilters(text="frontend"))
    it = await subscriptions.create(user_id, "IT", SearchFilters(text="it"))
    vacancy = make_vacancy("1", published_at=NOW)
    source = FakeSource({"frontend": [vacancy], "it": [vacancy]})

    await check_subscription(session, source, frontend.id, NOW)
    result = await check_subscription(session, source, it.id, NOW)

    assert result is not None
    # Вакансия привязана к обеим подпискам, но уведомление одно
    assert (result.new_matches, result.new_events) == (1, 0)
    assert await count(session, SubscriptionMatch) == 2
    assert await count(session, VacancyEvent) == 1


async def test_no_deliveries_for_inactive_channel(session: AsyncSession) -> None:
    user_id, channel_id = await setup_user(session)
    await UserRepository(session).deactivate_channel(channel_id)
    subscription = await SubscriptionRepository(session).create(
        user_id, "Frontend", SearchFilters(text="frontend")
    )
    source = FakeSource({"frontend": [make_vacancy("1", published_at=NOW)]})

    result = await check_subscription(session, source, subscription.id, NOW)

    assert result is not None
    assert result.new_events == 1
    assert await count(session, Delivery) == 0


async def test_paused_subscription_is_not_checked(session: AsyncSession) -> None:
    user_id, _ = await setup_user(session)
    subscriptions = SubscriptionRepository(session)
    subscription = await subscriptions.create(user_id, "Frontend", SearchFilters(text="x"))
    await subscriptions.set_active(subscription.id, False)
    source = FakeSource({})

    assert await check_subscription(session, source, subscription.id, NOW) is None
    assert source.calls == []


async def test_changing_filters_triggers_backfill_again(session: AsyncSession) -> None:
    user_id, _ = await setup_user(session)
    subscriptions = SubscriptionRepository(session)
    subscription = await subscriptions.create(user_id, "Frontend", SearchFilters(text="react"))
    source = FakeSource({"react": [], "vue": [make_vacancy("1", published_at=NOW)]})
    await check_subscription(session, source, subscription.id, NOW)

    # Те же фильтры — окно поиска не сбрасывается
    await subscriptions.update_filters(subscription.id, filters=SearchFilters(text="react"))
    assert subscription.last_checked_at == NOW

    await subscriptions.update_filters(subscription.id, filters=SearchFilters(text="vue"))
    later = NOW + timedelta(minutes=10)
    result = await check_subscription(session, source, subscription.id, later)

    assert result is not None
    assert (result.is_backfill, result.new_events) == (True, 1)
    assert source.calls[-1][1] == later - BACKFILL_PERIOD


async def test_rss_source_is_recorded(session: AsyncSession) -> None:
    user_id, _ = await setup_user(session)
    subscription = await SubscriptionRepository(session).create(
        user_id, "Frontend", SearchFilters(text="frontend")
    )
    source = FakeSource({"frontend": [make_vacancy("1", published_at=NOW)]})
    source.name = "rss"

    await check_subscription(session, source, subscription.id, NOW)

    vacancy = await session.get_one(Vacancy, "1")
    assert vacancy.first_seen_source == SourceName.RSS
