from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ChannelType, Delivery, DeliveryStatus, UserChannel
from app.db.repositories import SubscriptionRepository, UserRepository
from app.notifiers.base import ChannelUnavailableError, Message
from app.services.fetcher import check_subscription
from app.services.notifier import MAX_ATTEMPTS, process_delivery
from app.sources.base import SearchFilters
from app.workers.notifier import NOTIFIER_LOCK_KEY, send_notifications
from tests.factories import FakeRedis, FakeSource, make_vacancy

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


class FakeNotifier:
    def __init__(self, errors: list[Exception] | None = None) -> None:
        self.sent: list[tuple[str, Message]] = []
        self.errors = errors or []

    async def send(self, external_id: str, message: Message) -> None:
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append((external_id, message))


async def prepare(session: AsyncSession, *vacancies: str) -> tuple[int, list[tuple[int, int]]]:
    """Пользователь с подпиской, по которой найдены вакансии → ожидающие доставки."""
    user, channel = await UserRepository(session).get_or_create_by_channel(
        ChannelType.TELEGRAM, "785137155"
    )
    subscription = await SubscriptionRepository(session).create(
        user.id, "Frontend", SearchFilters(text="frontend")
    )
    found = [
        make_vacancy(vid, f"Вакансия {vid}", published_at=NOW.replace(minute=i))
        for i, vid in enumerate(vacancies)
    ]
    await check_subscription(session, FakeSource({"frontend": found}), subscription.id, NOW)
    deliveries = (await session.scalars(select(Delivery))).all()
    return channel.id, [(d.event_id, d.channel_id) for d in deliveries]


async def test_sent_delivery(session: AsyncSession) -> None:
    _, [(event_id, channel_id)] = await prepare(session, "1")
    notifier = FakeNotifier()

    status = await process_delivery(
        session, {ChannelType.TELEGRAM: notifier}, event_id, channel_id, NOW
    )

    assert status == DeliveryStatus.SENT
    [(chat_id, message)] = notifier.sent
    assert chat_id == "785137155"
    assert "Вакансия 1" in message.text
    assert "📦" in message.text  # первая проверка подписки — подгрузка за 24 часа
    assert "🔎 Frontend" in message.text


async def test_failed_delivery_is_retried_until_limit(session: AsyncSession) -> None:
    _, [(event_id, channel_id)] = await prepare(session, "1")
    notifier = FakeNotifier([TimeoutError()] * MAX_ATTEMPTS)
    notifiers = {ChannelType.TELEGRAM: notifier}

    statuses = [
        await process_delivery(session, notifiers, event_id, channel_id, NOW)
        for _ in range(MAX_ATTEMPTS)
    ]

    assert statuses == [DeliveryStatus.PENDING] * (MAX_ATTEMPTS - 1) + [DeliveryStatus.FAILED]


async def test_blocked_bot_deactivates_channel(session: AsyncSession) -> None:
    _, [(event_id, channel_id)] = await prepare(session, "1")
    notifier = FakeNotifier([ChannelUnavailableError("bot was blocked by the user")])

    status = await process_delivery(
        session, {ChannelType.TELEGRAM: notifier}, event_id, channel_id, NOW
    )

    assert status == DeliveryStatus.FAILED
    channel = await session.get_one(UserChannel, channel_id)
    assert channel.is_active is False


async def test_missing_notifier_leaves_delivery_pending(session: AsyncSession) -> None:
    _, [(event_id, channel_id)] = await prepare(session, "1")

    assert await process_delivery(session, {}, event_id, channel_id, NOW) is None
    delivery = await session.get_one(Delivery, (event_id, channel_id))
    assert delivery.status == DeliveryStatus.PENDING


def make_ctx(session: AsyncSession, notifier: FakeNotifier) -> dict[str, Any]:
    factory = async_sessionmaker(
        bind=session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    return {
        "redis": FakeRedis(),
        "session_factory": factory,
        "notifiers": {ChannelType.TELEGRAM: notifier},
    }


async def test_worker_sends_oldest_first_and_retries_later(session: AsyncSession) -> None:
    # Источник отдаёт сначала свежие (как hh.ru): "new" опубликована позже "old"
    await prepare(session, "old", "new")
    await session.commit()
    notifier = FakeNotifier([TimeoutError()])  # первая отправка упадёт
    ctx = make_ctx(session, notifier)

    first = await send_notifications(ctx)
    second = await send_notifications(ctx)

    assert first == {"pending": 1, "sent": 1}
    assert second == {"sent": 1}
    # "old" упала в первом запуске и ушла во втором
    assert [m.text.splitlines()[2] for _, m in notifier.sent] == [
        "<b>Вакансия new</b>",
        "<b>Вакансия old</b>",
    ]
    assert ctx["redis"].held == set()


async def test_worker_skips_when_already_running(session: AsyncSession) -> None:
    await prepare(session, "1")
    await session.commit()
    notifier = FakeNotifier()
    ctx = make_ctx(session, notifier)
    ctx["redis"].held.add(NOTIFIER_LOCK_KEY)

    assert await send_notifications(ctx) == {}
    assert notifier.sent == []
