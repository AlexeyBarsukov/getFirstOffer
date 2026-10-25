from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ChannelType, Subscription, VacancyEvent
from app.db.repositories import SubscriptionRepository, UserRepository
from app.sources.base import SearchFilters
from app.workers.fetcher import (
    CYCLE_LOCK_KEY,
    check_subscription_now,
    fetch_new_vacancies,
    subscription_lock_key,
)
from tests.factories import FakeAlerter, FakeRedis, FakeSource, make_vacancy

NOW = datetime.now(UTC)


def make_ctx(session: AsyncSession, source: FakeSource) -> dict[str, Any]:
    # Воркер открывает свои сессии и делает commit — в тестах они работают в savepoint
    factory = async_sessionmaker(
        bind=session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    return {
        "redis": FakeRedis(),
        "session_factory": factory,
        "source": source,
        "alerter": FakeAlerter(),
    }


async def create_subscriptions(session: AsyncSession, *texts: str) -> list[int]:
    user, _ = await UserRepository(session).get_or_create_by_channel(ChannelType.TELEGRAM, "1")
    subscriptions = SubscriptionRepository(session)
    ids = [(await subscriptions.create(user.id, t, SearchFilters(text=t))).id for t in texts]
    await session.commit()
    return ids


async def test_cycle_continues_after_failed_subscription(session: AsyncSession) -> None:
    broken_id, ok_id = await create_subscriptions(session, "broken", "ok")
    source = FakeSource(
        {"broken": RuntimeError("hh.ru 403"), "ok": [make_vacancy("1", published_at=NOW)]}
    )
    ctx = make_ctx(session, source)

    summary = await fetch_new_vacancies(ctx)

    assert summary == {"checked": 1, "not_checked": 1, "new_events": 1}
    session.expire_all()
    broken = await session.get_one(Subscription, broken_id)
    ok = await session.get_one(Subscription, ok_id)
    # Упавшая подписка не сдвигает окно поиска — следующий цикл повторит его
    assert broken.last_checked_at is None
    assert ok.last_checked_at is not None
    assert ctx["redis"].held == set()  # все блокировки сняты


async def test_cycle_skipped_while_previous_running(session: AsyncSession) -> None:
    await create_subscriptions(session, "ok")
    source = FakeSource({"ok": [make_vacancy("1", published_at=NOW)]})
    ctx = make_ctx(session, source)
    ctx["redis"].held.add(CYCLE_LOCK_KEY)

    summary = await fetch_new_vacancies(ctx)

    assert summary["checked"] == 0
    assert source.calls == []


async def test_busy_subscription_is_skipped_in_cycle(session: AsyncSession) -> None:
    busy_id, _ = await create_subscriptions(session, "busy", "ok")
    source = FakeSource({"ok": [make_vacancy("1", published_at=NOW)]})
    ctx = make_ctx(session, source)
    ctx["redis"].held.add(subscription_lock_key(busy_id))

    summary = await fetch_new_vacancies(ctx)

    assert summary == {"checked": 1, "not_checked": 1, "new_events": 1}
    assert [f.text for f, _ in source.calls] == ["ok"]


async def test_check_subscription_now(session: AsyncSession) -> None:
    (subscription_id,) = await create_subscriptions(session, "ok")
    source = FakeSource(
        {"ok": [make_vacancy("1", published_at=NOW), make_vacancy("2", published_at=NOW)]}
    )

    new_events = await check_subscription_now(make_ctx(session, source), subscription_id)

    assert new_events == 2
    assert await session.scalar(select(func.count()).select_from(VacancyEvent)) == 2


async def test_new_events_trigger_notifications_immediately(session: AsyncSession) -> None:
    _, empty_id = await create_subscriptions(session, "ok", "empty")
    ctx = make_ctx(session, FakeSource({"ok": [make_vacancy("1", published_at=NOW)]}))

    await fetch_new_vacancies(ctx)
    assert [name for name, _ in ctx["redis"].enqueued] == ["send_notifications"]

    # Нет новых вакансий — отправку не дёргаем
    ctx["redis"].enqueued.clear()
    await check_subscription_now(ctx, empty_id)
    assert ctx["redis"].enqueued == []


async def test_cycle_records_heartbeat_and_error(session: AsyncSession) -> None:
    await create_subscriptions(session, "broken")
    ctx = make_ctx(session, FakeSource({"broken": RuntimeError("hh.ru 403")}))

    await fetch_new_vacancies(ctx)

    redis = ctx["redis"]
    assert redis.values["monitor:fetcher_failed_cycles"] == b"1"
    assert b"hh.ru 403" in redis.values["monitor:fetcher_last_error"]
    assert "monitor:fetcher_last_cycle_at" in redis.values
