from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Delivery, DeliveryStatus, EventType, VacancyEvent


class EventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_new_vacancy_events(
        self, user_id: int, vacancy_ids: Sequence[str]
    ) -> list[VacancyEvent]:
        """События «новая вакансия». Для уже известных пользователю вакансий не создаются."""
        if not vacancy_ids:
            return []
        stmt = (
            insert(VacancyEvent)
            .values(
                [
                    {"user_id": user_id, "vacancy_id": vid, "type": EventType.NEW}
                    for vid in vacancy_ids
                ]
            )
            .on_conflict_do_nothing(
                index_elements=[VacancyEvent.user_id, VacancyEvent.vacancy_id],
                index_where=text("type = 'new'"),
            )
            .returning(VacancyEvent)
        )
        return list(await self._session.scalars(stmt))

    async def add_event(
        self, user_id: int, vacancy_id: str, type_: EventType, payload: dict[str, Any]
    ) -> VacancyEvent:
        event = VacancyEvent(user_id=user_id, vacancy_id=vacancy_id, type=type_, payload=payload)
        self._session.add(event)
        await self._session.flush()
        return event

    async def add_deliveries(self, event_id: int, channel_ids: Sequence[int]) -> None:
        if not channel_ids:
            return
        stmt = (
            insert(Delivery)
            .values([{"event_id": event_id, "channel_id": cid} for cid in channel_ids])
            .on_conflict_do_nothing()
        )
        await self._session.execute(stmt)

    async def pending_deliveries(self, limit: int) -> list[Delivery]:
        result = await self._session.scalars(
            select(Delivery)
            .where(Delivery.status == DeliveryStatus.PENDING)
            .order_by(Delivery.created_at, Delivery.event_id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(result)

    async def mark_sent(self, event_id: int, channel_id: int, sent_at: datetime) -> None:
        delivery = await self._session.get_one(Delivery, (event_id, channel_id))
        delivery.status = DeliveryStatus.SENT
        delivery.attempts += 1
        delivery.sent_at = sent_at
        delivery.error = None

    async def mark_failed(
        self, event_id: int, channel_id: int, error: str, *, max_attempts: int
    ) -> DeliveryStatus:
        """Учитывает неудачную попытку. Пока попытки не исчерпаны, доставка остаётся pending."""
        delivery = await self._session.get_one(Delivery, (event_id, channel_id))
        delivery.attempts += 1
        delivery.error = error
        if delivery.attempts >= max_attempts:
            delivery.status = DeliveryStatus.FAILED
        return delivery.status
