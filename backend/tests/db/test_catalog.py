from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ChannelType, MarkStatus, Subscription, VacancyEvent
from app.db.repositories import SubscriptionRepository, UserRepository
from app.db.repositories.catalog import CatalogRepository
from app.services.catalog import mark, next_card, sync_catalog
from app.services.fetcher import check_subscription
from app.services.subscriptions import SubscriptionService
from app.sources.base import SearchFilters
from app.sources.employers import employer_key
from app.workers.catalog import catalog_lock_key, sync_catalogs
from tests.factories import FakeRedis, FakeSource, make_vacancy

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


async def make_user(session: AsyncSession) -> int:
    user, _ = await UserRepository(session).get_or_create_by_channel(ChannelType.TELEGRAM, "1001")
    return user.id


async def catalog_sub(
    session: AsyncSession, user_id: int, text: str, exclude_words: list[str] | None = None
) -> Subscription:
    return await SubscriptionRepository(session).create(
        user_id, text, SearchFilters(text=text, areas=["113"]), exclude_words, catalog=True
    )


async def events_count(session: AsyncSession) -> int:
    return (await session.scalar(select(func.count()).select_from(VacancyEvent))) or 0


async def card_ids(session: AsyncSession, user_id: int) -> list[str]:
    """Порядок показа: отмечаем «позже» и смотрим следующую, пока не обойдём все."""
    seen: list[str] = []
    while (card := await next_card(session, user_id)) and card.item.vacancy.id not in seen:
        seen.append(card.item.vacancy.id)
        await mark(session, user_id, card.item.vacancy.id, MarkStatus.LATER)
    return seen


async def test_sync_is_silent_and_fills_catalog(session: AsyncSession) -> None:
    user_id = await make_user(session)
    subscription = await catalog_sub(session, user_id, "react", exclude_words=["lead"])
    source = FakeSource(
        {
            "react": [
                make_vacancy("1", "React developer", published_at=NOW),
                make_vacancy("2", "Frontend Team Lead", published_at=NOW),
                make_vacancy("3", "Frontend", published_at=NOW - timedelta(days=20)),
            ]
        }
    )

    result = await sync_catalog(session, source, subscription.id, NOW)

    assert result is not None
    assert (result.found, result.kept) == (3, 2)
    assert await events_count(session) == 0  # подборка не шлёт уведомлений
    assert subscription.catalog_synced_at == NOW
    assert subscription.last_checked_at == NOW  # следующая минутная проверка — только новые
    counts = await CatalogRepository(session).counts(user_id)
    assert (counts.total, counts.remaining) == (2, 2)


async def test_first_minute_check_of_catalog_is_silent(session: AsyncSession) -> None:
    user_id = await make_user(session)
    subscription = await catalog_sub(session, user_id, "react")
    source = FakeSource({"react": [make_vacancy("1", published_at=NOW)]})

    result = await check_subscription(session, source, subscription.id, NOW)

    assert result is not None and result.new_events == 0
    assert source.calls == []  # всю выдачу соберёт sync_catalog
    assert subscription.last_checked_at == NOW


async def test_new_vacancy_after_sync_is_notified_and_in_catalog(session: AsyncSession) -> None:
    user_id = await make_user(session)
    subscription = await catalog_sub(session, user_id, "react")
    source = FakeSource({"react": [make_vacancy("old", published_at=NOW - timedelta(days=3))]})
    await sync_catalog(session, source, subscription.id, NOW)

    source.responses["react"] = [make_vacancy("new", published_at=NOW + timedelta(minutes=1))]
    result = await check_subscription(session, source, subscription.id, NOW + timedelta(minutes=2))

    assert result is not None and result.new_events == 1
    assert sorted(await card_ids(session, user_id)) == ["new", "old"]


async def test_vacancy_missing_from_next_sync_leaves_catalog(session: AsyncSession) -> None:
    user_id = await make_user(session)
    subscription = await catalog_sub(session, user_id, "react")
    source = FakeSource(
        {"react": [make_vacancy("1", published_at=NOW), make_vacancy("2", published_at=NOW)]}
    )
    await sync_catalog(session, source, subscription.id, NOW)

    source.responses["react"] = [make_vacancy("1", published_at=NOW)]
    await sync_catalog(session, source, subscription.id, NOW + timedelta(hours=2))

    assert await card_ids(session, user_id) == ["1"]


async def test_order_priority_marks_and_later(session: AsyncSession) -> None:
    user_id = await make_user(session)
    remote = await catalog_sub(session, user_id, "remote")
    kazan = await catalog_sub(session, user_id, "kazan")
    source = FakeSource(
        {
            "remote": [
                make_vacancy("r-new", published_at=NOW),
                make_vacancy("r-old", published_at=NOW - timedelta(days=5)),
            ],
            "kazan": [make_vacancy("k-new", published_at=NOW + timedelta(hours=1))],
        }
    )
    await sync_catalog(session, source, remote.id, NOW)
    await sync_catalog(session, source, kazan.id, NOW)

    # Первая подписка (удалёнка) важнее, внутри — свежие первыми
    card = await next_card(session, user_id)
    assert card is not None and card.item.vacancy.id == "r-new"
    assert card.item.subscription_names == ["remote"]

    await mark(session, user_id, "r-new", MarkStatus.APPLIED)
    await mark(session, user_id, "r-old", MarkStatus.LATER)
    card = await next_card(session, user_id)
    assert card is not None and card.item.vacancy.id == "k-new"
    assert (card.counts.total, card.counts.applied, card.counts.remaining) == (3, 1, 2)

    await mark(session, user_id, "k-new", MarkStatus.SKIPPED)
    card = await next_card(session, user_id)
    # Отложенная вернулась, когда остальное разобрано
    assert card is not None and card.item.vacancy.id == "r-old"
    assert card.item.mark == MarkStatus.LATER

    await mark(session, user_id, "r-old", MarkStatus.SKIPPED)
    assert await next_card(session, user_id) is None


async def test_hidden_company_and_new_stop_word_apply_immediately(session: AsyncSession) -> None:
    user_id = await make_user(session)
    subscription = await catalog_sub(session, user_id, "react")
    source = FakeSource(
        {
            "react": [
                make_vacancy("1", "React", published_at=NOW, employer_id="10"),
                make_vacancy("2", "React senior", published_at=NOW, employer_id="20"),
                make_vacancy("3", "Vue", published_at=NOW, employer_id="30"),
            ]
        }
    )
    await sync_catalog(session, source, subscription.id, NOW)

    await SubscriptionService(session).hide_employer(user_id, employer_key("Компания 10"))
    await SubscriptionRepository(session).update_filters(subscription.id, exclude_words=["senior"])

    assert await card_ids(session, user_id) == ["3"]


async def test_sync_catalogs_worker(session: AsyncSession) -> None:
    user_id = await make_user(session)
    ok = await catalog_sub(session, user_id, "ok")
    busy = await catalog_sub(session, user_id, "busy")
    await SubscriptionRepository(session).create(user_id, "notify", SearchFilters(text="notify"))
    await session.commit()
    redis = FakeRedis()
    redis.held.add(catalog_lock_key(busy.id))
    source = FakeSource({"ok": [make_vacancy("1", published_at=NOW)]})
    ctx: dict[str, Any] = {
        "redis": redis,
        "source": source,
        "session_factory": async_sessionmaker(
            bind=session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ),
    }

    summary = await sync_catalogs(ctx)

    assert summary == {"subscriptions": 2, "synced": 1}
    assert [f.text for f in source.full_calls] == ["ok"]
    await session.refresh(ok)
    assert ok.catalog_synced_at is not None
