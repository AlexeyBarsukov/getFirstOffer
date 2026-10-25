from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ChannelType,
    Delivery,
    DeliveryStatus,
    EventType,
    SourceName,
    SubscriptionMatch,
    Vacancy,
    VacancyStatus,
)
from app.db.repositories import (
    EventRepository,
    SubscriptionRepository,
    UserRepository,
    VacancyRepository,
)
from app.services.presets import PRESETS
from app.sources.hh_models import VacancySearchPage, VacancyShort
from tests.conftest import load_fixture

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def fixture_vacancies() -> list[VacancyShort]:
    return VacancySearchPage.model_validate(load_fixture("hh/vacancies_page_0.json")).items


async def make_user_with_subscription(session: AsyncSession) -> tuple[int, int, int]:
    user, channel = await UserRepository(session).get_or_create_by_channel(
        ChannelType.TELEGRAM, "1001"
    )
    preset = PRESETS["frontend"]
    subscription = await SubscriptionRepository(session).create(
        user.id, preset.title, preset.filters, preset.exclude_words
    )
    return user.id, channel.id, subscription.id


async def test_get_or_create_by_channel_is_idempotent(session: AsyncSession) -> None:
    users = UserRepository(session)

    user, channel = await users.get_or_create_by_channel(ChannelType.TELEGRAM, "42")
    await users.deactivate_channel(channel.id)
    same_user, same_channel = await users.get_or_create_by_channel(ChannelType.TELEGRAM, "42")

    assert (same_user.id, same_channel.id) == (user.id, channel.id)
    # Повторный /start после блокировки бота снова включает канал
    assert same_channel.is_active is True
    assert [c.id for c in await users.active_channels(user.id)] == [channel.id]


async def test_email_is_normalized_and_unique(session: AsyncSession) -> None:
    users = UserRepository(session)
    first, _ = await users.get_or_create_by_channel(ChannelType.TELEGRAM, "1")
    second, _ = await users.get_or_create_by_channel(ChannelType.TELEGRAM, "2")

    await users.set_email(first.id, "  Lesha@Example.COM ")
    found = await users.get_by_email("lesha@example.com")
    assert found is not None
    assert found.id == first.id

    await users.set_email(second.id, "lesha@example.com")
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_subscription_stores_filters_and_exclusions(session: AsyncSession) -> None:
    _, _, subscription_id = await make_user_with_subscription(session)
    subscriptions = SubscriptionRepository(session)

    await subscriptions.add_excluded_employer(subscription_id, "1740")
    await subscriptions.add_excluded_employer(subscription_id, "1740")
    await session.commit()
    session.expire_all()

    subscription = await subscriptions.get(subscription_id)
    assert subscription is not None
    assert subscription.search_filters == PRESETS["frontend"].filters
    assert subscription.exclude_employers == ["1740"]
    assert "senior" in subscription.exclude_words


async def test_list_active_skips_paused(session: AsyncSession) -> None:
    user_id, _, subscription_id = await make_user_with_subscription(session)
    subscriptions = SubscriptionRepository(session)
    other = await subscriptions.create(user_id, "Backend", PRESETS["backend"].filters)

    await subscriptions.set_active(subscription_id, False)

    assert [s.id for s in await subscriptions.list_active()] == [other.id]
    assert len(await subscriptions.list_for_user(user_id)) == 2


async def test_upsert_keeps_first_seen_and_updates_fields(session: AsyncSession) -> None:
    vacancies = VacancyRepository(session)
    first, _ = fixture_vacancies()

    await vacancies.upsert_many([first], SourceName.PARSER)
    stored = await vacancies.get(first.id)
    assert stored is not None
    first_seen_at = stored.first_seen_at
    assert stored.raw["snippet"]["requirement"].startswith("Знание")  # полный ответ сохранён

    assert first.salary is not None
    first.salary.from_ = 150000
    await vacancies.upsert_many([first], SourceName.API)
    session.expire_all()

    stored = await vacancies.get(first.id)
    assert stored is not None
    assert stored.salary_from == 150000
    assert stored.first_seen_source == SourceName.PARSER
    assert stored.first_seen_at == first_seen_at
    assert stored.status == VacancyStatus.OPEN


async def test_add_matches_returns_only_new(session: AsyncSession) -> None:
    _, _, subscription_id = await make_user_with_subscription(session)
    vacancies = VacancyRepository(session)
    items = fixture_vacancies()
    await vacancies.upsert_many(items, SourceName.API)

    first_run = await vacancies.add_matches(subscription_id, [items[0].id], is_backfill=True)
    second_run = await vacancies.add_matches(subscription_id, [v.id for v in items])

    assert first_run == [items[0].id]
    assert second_run == [items[1].id]


async def test_new_vacancy_event_created_once_per_user(session: AsyncSession) -> None:
    user_id, _, _ = await make_user_with_subscription(session)
    await VacancyRepository(session).upsert_many(fixture_vacancies(), SourceName.API)
    events = EventRepository(session)

    created = await events.add_new_vacancy_events(user_id, ["110000001", "110000002"])
    # Та же вакансия пришла из второй подписки — повторного события нет
    again = await events.add_new_vacancy_events(user_id, ["110000001"])
    # Изменения той же вакансии — отдельные события, их может быть много
    change = await events.add_event(
        user_id, "110000001", EventType.SALARY_CHANGED, {"salary_from": {"old": 1, "new": 2}}
    )

    assert sorted(e.vacancy_id for e in created) == ["110000001", "110000002"]
    assert all(e.type == EventType.NEW for e in created)
    assert again == []
    assert change.id is not None


async def test_delivery_lifecycle(session: AsyncSession) -> None:
    user_id, channel_id, _ = await make_user_with_subscription(session)
    await VacancyRepository(session).upsert_many(fixture_vacancies(), SourceName.API)
    events = EventRepository(session)
    sent_event, failing_event = await events.add_new_vacancy_events(
        user_id, ["110000001", "110000002"]
    )
    await events.add_deliveries(sent_event.id, [channel_id])
    await events.add_deliveries(sent_event.id, [channel_id])  # повтор не дублирует
    await events.add_deliveries(failing_event.id, [channel_id])

    pending = await events.pending_deliveries(limit=10)
    assert {d.event_id for d in pending} == {sent_event.id, failing_event.id}

    await events.mark_sent(sent_event.id, channel_id, NOW)
    first = await events.mark_failed(failing_event.id, channel_id, "timeout", max_attempts=2)
    second = await events.mark_failed(failing_event.id, channel_id, "timeout", max_attempts=2)

    assert (first, second) == (DeliveryStatus.PENDING, DeliveryStatus.FAILED)
    assert await events.pending_deliveries(limit=10) == []
    sent = await session.get_one(Delivery, (sent_event.id, channel_id))
    assert (sent.status, sent.attempts, sent.sent_at) == (DeliveryStatus.SENT, 1, NOW)


async def test_tracking_queue_and_snapshots(session: AsyncSession) -> None:
    _, _, subscription_id = await make_user_with_subscription(session)
    vacancies = VacancyRepository(session)
    fresh, archived = fixture_vacancies()
    await vacancies.upsert_many([fresh, archived], SourceName.API)
    await vacancies.add_matches(subscription_id, [fresh.id, archived.id])
    await vacancies.mark_checked(archived.id, NOW, VacancyStatus.ARCHIVED)

    queue = await vacancies.list_for_tracking(NOW - timedelta(days=30), limit=10)
    assert [v.id for v in queue] == [fresh.id]

    await vacancies.add_snapshot(fresh.id, {"salary_from": 80000})
    await vacancies.add_snapshot(fresh.id, {"salary_from": 120000})
    latest = await vacancies.latest_snapshot(fresh.id)
    assert latest is not None
    assert latest.data == {"salary_from": 120000}


async def test_deleting_subscription_cascades_matches(session: AsyncSession) -> None:
    _, _, subscription_id = await make_user_with_subscription(session)
    vacancies = VacancyRepository(session)
    items = fixture_vacancies()
    await vacancies.upsert_many(items, SourceName.API)
    await vacancies.add_matches(subscription_id, [v.id for v in items])

    await SubscriptionRepository(session).delete(subscription_id)

    # Вакансии остаются, исчезают только связи с подпиской
    assert len((await session.scalars(select(Vacancy))).all()) == 2
    matches = await session.scalars(
        select(SubscriptionMatch).where(SubscriptionMatch.subscription_id == subscription_id)
    )
    assert matches.all() == []
