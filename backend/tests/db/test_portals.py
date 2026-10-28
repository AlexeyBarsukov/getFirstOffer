"""Несколько площадок: у каждой своё окно поиска, своя синхронизация подборки и свои ошибки."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ChannelType, MarkStatus, Subscription, Vacancy
from app.db.repositories import SubscriptionRepository, UserRepository
from app.db.repositories.catalog import CatalogRepository
from app.services.catalog import mark, next_card, sync_catalog
from app.services.fetcher import BACKFILL_PERIOD, check_subscription
from app.sources.base import SearchFilters
from app.sources.portals import Portal
from app.sources.registry import PortalSources
from app.workers.catalog import sync_catalog_now
from app.workers.fetcher import check_subscription_now
from tests.factories import FakeAlerter, FakeRedis, FakeSource, make_vacancy, portal_state

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


async def make_subscription(
    session: AsyncSession, *, catalog: bool = False, portals: list[str] | None = None
) -> Subscription:
    user, _ = await UserRepository(session).get_or_create_by_channel(ChannelType.TELEGRAM, "1")
    subscription = await SubscriptionRepository(session).create(
        user.id, "react", SearchFilters(text="react"), catalog=catalog
    )
    if portals is not None:
        subscription.portals = portals
    return subscription


async def test_each_portal_has_own_search_window(session: AsyncSession) -> None:
    subscription = await make_subscription(session)
    hh = FakeSource({"react": [make_vacancy("1", published_at=NOW)]})
    habr = FakeSource({"react": [make_vacancy("habr-7", published_at=NOW)]}, portal=Portal.HABR)

    await check_subscription(session, hh, subscription.id, NOW)
    # Хабр подключили позже — он подгружает свои 24 часа, hh.ru это не трогает
    later = NOW + timedelta(hours=3)
    result = await check_subscription(session, habr, subscription.id, later)

    assert result is not None and result.is_backfill and result.new_events == 1
    assert habr.calls[0][1] == later - BACKFILL_PERIOD
    assert (await portal_state(session, subscription.id)).last_checked_at == NOW
    assert (await portal_state(session, subscription.id, Portal.HABR)).last_checked_at == later
    vacancy = await session.get_one(Vacancy, "habr-7")
    assert vacancy.portal == Portal.HABR


async def test_portal_disabled_in_subscription_is_skipped(session: AsyncSession) -> None:
    subscription = await make_subscription(session, portals=["hh"])
    habr = FakeSource({"react": [make_vacancy("habr-7", published_at=NOW)]}, portal=Portal.HABR)

    assert await check_subscription(session, habr, subscription.id, NOW) is None
    assert habr.calls == []


async def test_changing_filters_resets_every_portal(session: AsyncSession) -> None:
    subscription = await make_subscription(session)
    subscriptions = SubscriptionRepository(session)
    for portal in (Portal.HH, Portal.HABR):
        await subscriptions.mark_checked(subscription.id, portal, NOW)
    assert not await subscriptions.is_unchecked(subscription.id)

    await subscriptions.update_filters(subscription.id, filters=SearchFilters(text="vue"))

    assert await subscriptions.is_unchecked(subscription.id)


async def test_catalog_keeps_portals_apart(session: AsyncSession) -> None:
    subscription = await make_subscription(session, catalog=True)
    user_id = subscription.user_id
    hh = FakeSource({"react": [make_vacancy("1", published_at=NOW)]})
    habr = FakeSource(
        {"react": [make_vacancy("habr-7", published_at=NOW - timedelta(hours=1))]},
        portal=Portal.HABR,
    )
    await sync_catalog(session, hh, subscription.id, NOW)
    await sync_catalog(session, habr, subscription.id, NOW + timedelta(minutes=1))
    assert (await CatalogRepository(session).counts(user_id)).total == 2

    # Следующая синхронизация hh.ru позже Хабра — вакансия Хабра из подборки не пропадает
    await sync_catalog(session, hh, subscription.id, NOW + timedelta(hours=2))
    assert (await CatalogRepository(session).counts(user_id)).total == 2

    # Хабр выключили в подписке — его вакансии из подборки уходят
    subscription.portals = ["hh"]
    await session.flush()
    card = await next_card(session, user_id)
    assert card is not None and card.item.vacancy.id == "1"
    await mark(session, user_id, "1", MarkStatus.SKIPPED)
    assert await next_card(session, user_id) is None


def make_ctx(session: AsyncSession, portals: dict[Portal, PortalSources]) -> dict[str, Any]:
    return {
        "redis": FakeRedis(),
        "alerter": FakeAlerter(),
        "portals": portals,
        "session_factory": async_sessionmaker(
            bind=session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ),
    }


async def test_broken_portal_does_not_block_others(session: AsyncSession) -> None:
    subscription = await make_subscription(session, catalog=True)
    subscription_id = subscription.id
    await session.commit()
    hh = FakeSource({"react": RuntimeError("hh.ru 403")})
    habr = FakeSource({"react": [make_vacancy("habr-7", published_at=NOW)]}, portal=Portal.HABR)
    ctx = make_ctx(
        session, {Portal.HH: PortalSources(search=hh), Portal.HABR: PortalSources(search=habr)}
    )

    assert await sync_catalog_now(ctx, subscription_id) == 1
    # Минутная проверка подборки после синхронизации — только новые, ошибка hh.ru не мешает Хабру
    assert await check_subscription_now(ctx, subscription_id) == 0
    assert [f.text for f, _ in habr.calls] == ["react"]
    assert ctx["redis"].held == set()
