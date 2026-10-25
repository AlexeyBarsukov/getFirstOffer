from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Subscription
from app.sources.base import SearchFilters


class SubscriptionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        user_id: int,
        name: str,
        filters: SearchFilters,
        exclude_words: list[str] | None = None,
        exclude_employers: list[str] | None = None,
    ) -> Subscription:
        subscription = Subscription(
            user_id=user_id,
            name=name,
            filters=filters.model_dump(mode="json"),
            exclude_words=exclude_words or [],
            exclude_employers=exclude_employers or [],
            is_active=True,
        )
        self._session.add(subscription)
        await self._session.flush()
        return subscription

    async def get(self, subscription_id: int) -> Subscription | None:
        return await self._session.get(Subscription, subscription_id)

    async def list_for_user(self, user_id: int) -> list[Subscription]:
        result = await self._session.scalars(
            select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.id)
        )
        return list(result)

    async def list_active(self) -> list[Subscription]:
        result = await self._session.scalars(
            select(Subscription).where(Subscription.is_active.is_(True)).order_by(Subscription.id)
        )
        return list(result)

    async def update_filters(
        self,
        subscription_id: int,
        *,
        name: str | None = None,
        filters: SearchFilters | None = None,
        exclude_words: list[str] | None = None,
        exclude_employers: list[str] | None = None,
    ) -> Subscription:
        subscription = await self._session.get_one(Subscription, subscription_id)
        if name is not None:
            subscription.name = name
        if filters is not None:
            new_filters = filters.model_dump(mode="json")
            if new_filters != subscription.filters:
                subscription.filters = new_filters
                # Новые фильтры — следующая проверка снова подгрузит вакансии за 24 часа
                subscription.last_checked_at = None
        if exclude_words is not None:
            subscription.exclude_words = exclude_words
        if exclude_employers is not None:
            subscription.exclude_employers = exclude_employers
        await self._session.flush()
        return subscription

    async def add_excluded_employer(self, subscription_id: int, employer_id: str) -> None:
        subscription = await self._session.get_one(Subscription, subscription_id)
        if employer_id not in subscription.exclude_employers:
            # Новый список, чтобы SQLAlchemy увидел изменение ARRAY
            subscription.exclude_employers = [*subscription.exclude_employers, employer_id]

    async def remove_excluded_employer(self, subscription_id: int, employer_id: str) -> None:
        subscription = await self._session.get_one(Subscription, subscription_id)
        if employer_id in subscription.exclude_employers:
            subscription.exclude_employers = [
                e for e in subscription.exclude_employers if e != employer_id
            ]

    async def set_active(self, subscription_id: int, is_active: bool) -> None:
        subscription = await self._session.get_one(Subscription, subscription_id)
        subscription.is_active = is_active

    async def mark_checked(self, subscription_id: int, checked_at: datetime) -> None:
        subscription = await self._session.get_one(Subscription, subscription_id)
        subscription.last_checked_at = checked_at

    async def delete(self, subscription_id: int) -> None:
        await self._session.execute(delete(Subscription).where(Subscription.id == subscription_id))
