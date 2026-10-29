from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, any_, distinct, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.db.models import (
    MarkStatus,
    Subscription,
    SubscriptionMatch,
    SubscriptionPortalState,
    Vacancy,
    VacancyMark,
    VacancyStatus,
)


@dataclass(frozen=True)
class CatalogItem:
    vacancy: Vacancy
    mark: MarkStatus | None
    subscription_names: list[str]


@dataclass(frozen=True)
class CatalogCounts:
    total: int
    applied: int
    skipped: int

    @property
    def remaining(self) -> int:
        return self.total - self.applied - self.skipped


class CatalogRepository:
    """Полная подборка пользователя: вакансии из последней выдачи его подписок-подборок."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _in_catalog(self, user_id: int) -> list[object]:
        return [
            Subscription.user_id == user_id,
            Subscription.catalog.is_(True),
            Subscription.is_active.is_(True),
            # Площадка вакансии не выключена в подписке
            or_(Subscription.portals == [], Vacancy.portal == any_(Subscription.portals)),
            SubscriptionPortalState.catalog_synced_at.is_not(None),
            # Вакансия была в последней выдаче своей площадки (или пришла новой уже после неё)
            SubscriptionMatch.last_seen_at >= SubscriptionPortalState.catalog_synced_at,
            Vacancy.status == VacancyStatus.OPEN,
        ]

    @staticmethod
    def _state_join() -> ColumnElement[bool]:
        return and_(
            SubscriptionPortalState.subscription_id == Subscription.id,
            SubscriptionPortalState.portal == Vacancy.portal,
        )

    def _base(self, user_id: int) -> Select[Vacancy, MarkStatus]:
        return (
            select(Vacancy, VacancyMark.status)
            .join(SubscriptionMatch, SubscriptionMatch.vacancy_id == Vacancy.id)
            .join(Subscription, Subscription.id == SubscriptionMatch.subscription_id)
            .join(SubscriptionPortalState, self._state_join())
            .outerjoin(
                VacancyMark,
                and_(VacancyMark.vacancy_id == Vacancy.id, VacancyMark.user_id == user_id),
            )
            .where(*self._in_catalog(user_id))  # type: ignore[arg-type]
        )

    async def candidates(self, user_id: int, limit: int) -> list[CatalogItem]:
        """Неразобранные вакансии по порядку показа.

        Сначала без отметки, потом отложенные «на потом» (в порядке откладывания).
        Внутри — по порядку подписок (первая созданная подписка — самая приоритетная),
        затем свежие первыми.
        """
        names = func.array_agg(distinct(Subscription.name))
        stmt = (
            self._base(user_id)
            .add_columns(names)
            .where(or_(VacancyMark.status.is_(None), VacancyMark.status == MarkStatus.LATER))
            .group_by(Vacancy.id, VacancyMark.status, VacancyMark.updated_at)
            .order_by(
                VacancyMark.status.is_not(None),
                VacancyMark.updated_at.asc().nulls_first(),
                func.min(Subscription.id),
                Vacancy.published_at.desc(),
            )
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).all()
        return [CatalogItem(vacancy, mark, sorted(subs)) for vacancy, mark, subs in rows]

    async def counts(self, user_id: int) -> CatalogCounts:
        """Дубли с разных площадок считаются одной вакансией."""
        group = func.coalesce(Vacancy.duplicate_of, Vacancy.id)
        stmt = (
            select(
                func.count(distinct(group)),
                func.count(distinct(group)).filter(VacancyMark.status == MarkStatus.APPLIED),
                func.count(distinct(group)).filter(VacancyMark.status == MarkStatus.SKIPPED),
            )
            .select_from(Vacancy)
            .join(SubscriptionMatch, SubscriptionMatch.vacancy_id == Vacancy.id)
            .join(Subscription, Subscription.id == SubscriptionMatch.subscription_id)
            .join(SubscriptionPortalState, self._state_join())
            .outerjoin(
                VacancyMark,
                and_(VacancyMark.vacancy_id == Vacancy.id, VacancyMark.user_id == user_id),
            )
            .where(*self._in_catalog(user_id))  # type: ignore[arg-type]
        )
        total, applied, skipped = (await self._session.execute(stmt)).one()
        return CatalogCounts(total=total, applied=applied, skipped=skipped)

    async def set_mark(self, user_id: int, vacancy_id: str, status: MarkStatus) -> None:
        """Отметка ставится на всю группу дублей: откликнулся на hh.ru — значит, и на Хабре."""
        vacancy = await self._session.get_one(Vacancy, vacancy_id)
        group_id = vacancy.group_id
        ids = list(
            await self._session.scalars(
                select(Vacancy.id).where(
                    or_(Vacancy.id == group_id, Vacancy.duplicate_of == group_id)
                )
            )
        )
        stmt = insert(VacancyMark).values(
            [{"user_id": user_id, "vacancy_id": vid, "status": status} for vid in ids]
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[VacancyMark.user_id, VacancyMark.vacancy_id],
            set_={"status": stmt.excluded.status, "updated_at": func.now()},
        )
        await self._session.execute(stmt)

    async def has_catalog(self, user_id: int) -> bool:
        stmt = select(Subscription.id).where(
            Subscription.user_id == user_id,
            Subscription.catalog.is_(True),
            Subscription.is_active.is_(True),
        )
        return (await self._session.scalar(stmt.limit(1))) is not None
