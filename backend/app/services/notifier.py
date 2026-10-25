"""Отправка одной доставки (событие → канал). Транзакцией управляет воркер."""

from collections.abc import Mapping
from datetime import datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChannelType, DeliveryStatus, UserChannel, Vacancy
from app.db.repositories import EventRepository, UserRepository, VacancyRepository
from app.notifiers.base import ChannelUnavailableError, Notifier
from app.notifiers.messages import build_message

logger = structlog.get_logger(__name__)

# Попыток на одну доставку; повтор — в следующем цикле отправки (раз в минуту или раньше)
MAX_ATTEMPTS = 5


async def process_delivery(
    session: AsyncSession,
    notifiers: Mapping[ChannelType, Notifier],
    event_id: int,
    channel_id: int,
    now: datetime,
) -> DeliveryStatus | None:
    """Пытается отправить.

    Возвращает новый статус доставки или None, если для типа канала нет отправителя.
    """
    events = EventRepository(session)
    channel = await session.get_one(UserChannel, channel_id)
    notifier = notifiers.get(channel.type)
    if notifier is None:
        # Например, не задан TELEGRAM_BOT_TOKEN. Доставка ждёт, пока канал не настроят
        logger.warning("notifier_not_configured", channel_type=channel.type)
        return None
    if not channel.is_active:
        await events.mark_undeliverable(event_id, channel_id, "channel is inactive")
        return DeliveryStatus.FAILED

    event = await events.get_event(event_id)
    vacancy = await session.get_one(Vacancy, event.vacancy_id)
    matches = await VacancyRepository(session).user_matches(event.user_id, event.vacancy_id)
    message = build_message(event, vacancy, matches)

    try:
        await notifier.send(channel.external_id, message)
    except ChannelUnavailableError as exc:
        logger.warning("channel_unavailable", channel_id=channel_id, error=str(exc))
        await UserRepository(session).deactivate_channel(channel_id)
        await events.mark_undeliverable(event_id, channel_id, str(exc))
        return DeliveryStatus.FAILED
    except Exception as exc:
        status = await events.mark_failed(
            event_id, channel_id, repr(exc), max_attempts=MAX_ATTEMPTS
        )
        logger.warning(
            "delivery_failed",
            event_id=event_id,
            channel_id=channel_id,
            error=repr(exc),
            will_retry=status == DeliveryStatus.PENDING,
        )
        return status

    await events.mark_sent(event_id, channel_id, now)
    logger.info("delivery_sent", event_id=event_id, channel_id=channel_id, type=event.type)
    return DeliveryStatus.SENT
