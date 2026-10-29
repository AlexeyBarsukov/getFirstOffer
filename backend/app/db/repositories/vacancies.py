from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    EventType,
    MarkStatus,
    SourceName,
    Subscription,
    SubscriptionMatch,
    Vacancy,
    VacancyEvent,
    VacancyMark,
    VacancySnapshot,
    VacancyStatus,
)
from app.sources.dedup import dedup_key
from app.sources.details import VacancyDetails
from app.sources.models import VacancyShort
from app.sources.portals import portal_of


def vacancy_row(vacancy: VacancyShort) -> dict[str, Any]:
    salary = vacancy.salary
    return {
        "id": vacancy.id,
        "portal": portal_of(vacancy.id),
        "name": vacancy.name,
        "employer_id": vacancy.employer.id,
        "employer_name": vacancy.employer.name,
        "area": vacancy.area.name,
        "salary_from": salary.from_ if salary else None,
        "salary_to": salary.to if salary else None,
        "salary_currency": salary.currency if salary else None,
        "salary_gross": salary.gross if salary else None,
        "url": vacancy.alternate_url,
        "published_at": vacancy.published_at,
        "raw": vacancy.model_dump(mode="json", by_alias=True),
        # У анонимного работодателя id нет — по названию «Компания скрыта» не сравниваем
        "dedup_key": dedup_key(vacancy.employer.name, vacancy.name)
        if vacancy.employer.id
        else None,
    }


# Поля, которые обновляются при повторной встрече вакансии в выдаче.
# first_seen_* не трогаем никогда, status ведёт трекер изменений.
_UPSERT_FIELDS = (
    "name",
    "employer_id",
    "employer_name",
    "area",
    "salary_from",
    "salary_to",
    "salary_currency",
    "salary_gross",
    "url",
    "published_at",
    "raw",
    "dedup_key",
)


# Дубль — та же вакансия на другой площадке: первая ещё открыта или опубликованы
# не дальше чем через 2 недели (закрытую давно вакансию компания могла открыть заново)
DUPLICATE_WINDOW = timedelta(days=14)


class VacancyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, vacancy_id: str) -> Vacancy | None:
        return await self._session.get(Vacancy, vacancy_id)

    async def upsert_many(self, vacancies: Sequence[VacancyShort], source: SourceName) -> None:
        if not vacancies:
            return
        rows = [vacancy_row(v) | {"first_seen_source": source} for v in vacancies]
        stmt = insert(Vacancy).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Vacancy.id],
            set_={field: stmt.excluded[field] for field in _UPSERT_FIELDS},
        )
        await self._session.execute(stmt)

    async def link_duplicates(self, vacancy_ids: Sequence[str]) -> None:
        """Связать вакансии с их копиями на других площадках.

        Первая увиденная вакансия группы остаётся главной (duplicate_of = None), остальные
        ссылаются на неё. Дубли внутри одной площадки не ищем: компания может искать людей
        с одинаковой должностью в разные города.
        """
        if not vacancy_ids:
            return
        unlinked = await self._session.scalars(
            select(Vacancy).where(
                Vacancy.id.in_(vacancy_ids),
                Vacancy.duplicate_of.is_(None),
                Vacancy.dedup_key.is_not(None),
            )
        )
        for vacancy in list(unlinked):
            root = await self._session.scalar(
                select(Vacancy.id)
                .where(
                    Vacancy.dedup_key == vacancy.dedup_key,
                    Vacancy.portal != vacancy.portal,
                    Vacancy.duplicate_of.is_(None),
                    Vacancy.first_seen_at <= vacancy.first_seen_at,
                    or_(
                        Vacancy.status == VacancyStatus.OPEN,
                        Vacancy.published_at.between(
                            vacancy.published_at - DUPLICATE_WINDOW,
                            vacancy.published_at + DUPLICATE_WINDOW,
                        ),
                    ),
                )
                .order_by(Vacancy.first_seen_at, Vacancy.id)
                .limit(1)
            )
            # Главную вакансию, у которой уже есть дубли, саму в дубли не записываем
            if root is not None and not await self._has_duplicates(vacancy.id):
                vacancy.duplicate_of = root
        await self._session.flush()

    async def _has_duplicates(self, vacancy_id: str) -> bool:
        found = await self._session.scalar(
            select(Vacancy.id).where(Vacancy.duplicate_of == vacancy_id).limit(1)
        )
        return found is not None

    async def notified_duplicates(self, user_id: int, vacancy_ids: Sequence[str]) -> set[str]:
        """Вакансии, о главной вакансии группы которых пользователь уже получил уведомление."""
        if not vacancy_ids:
            return set()
        result = await self._session.scalars(
            select(Vacancy.id)
            .join(VacancyEvent, VacancyEvent.vacancy_id == Vacancy.duplicate_of)
            .where(
                Vacancy.id.in_(vacancy_ids),
                VacancyEvent.user_id == user_id,
                VacancyEvent.type == EventType.NEW,
            )
        )
        return set(result)

    async def group(self, vacancy: Vacancy) -> list[Vacancy]:
        """Все открытые вакансии группы дублей, главная первой."""
        group_id = vacancy.group_id
        result = await self._session.scalars(
            select(Vacancy)
            .where(
                or_(Vacancy.id == group_id, Vacancy.duplicate_of == group_id),
                Vacancy.status == VacancyStatus.OPEN,
            )
            .order_by(Vacancy.duplicate_of.is_not(None), Vacancy.first_seen_at, Vacancy.id)
        )
        return list(result)

    async def add_matches(
        self,
        subscription_id: int,
        vacancy_ids: Sequence[str],
        *,
        is_backfill: bool = False,
        seen_at: datetime | None = None,
    ) -> list[str]:
        """Связывает вакансии с подпиской.

        Возвращает id вакансий, которых в подписке ещё не было.
        seen_at — время по часам приложения: с ним сравнивается время синхронизации подборки
        (без него ставится now() базы).
        """
        if not vacancy_ids:
            return []
        times = {"matched_at": seen_at, "last_seen_at": seen_at} if seen_at else {}
        stmt = (
            insert(SubscriptionMatch)
            .values(
                [
                    {
                        "subscription_id": subscription_id,
                        "vacancy_id": vid,
                        "is_backfill": is_backfill,
                        **times,
                    }
                    for vid in vacancy_ids
                ]
            )
            .on_conflict_do_nothing()
            .returning(SubscriptionMatch.vacancy_id)
        )
        return list(await self._session.scalars(stmt))

    async def touch_matches(
        self, subscription_id: int, vacancy_ids: Sequence[str], seen_at: datetime
    ) -> None:
        """Связать вакансии с подпиской без уведомлений и отметить, что они есть в выдаче.

        Для полной подборки: вакансии, которых не было в последней выдаче, из неё пропадают.
        """
        if not vacancy_ids:
            return
        stmt = insert(SubscriptionMatch).values(
            [
                {
                    "subscription_id": subscription_id,
                    "vacancy_id": vid,
                    "is_backfill": True,
                    "last_seen_at": seen_at,
                }
                for vid in vacancy_ids
            ]
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[SubscriptionMatch.subscription_id, SubscriptionMatch.vacancy_id],
            set_={"last_seen_at": stmt.excluded.last_seen_at},
        )
        await self._session.execute(stmt)

    async def list_for_tracking(self, matched_since: datetime, limit: int) -> list[Vacancy]:
        """Открытые вакансии, за изменениями которых стоит следить. Давно не проверенные — первыми.

        Это вакансии, о которых пользователь получил уведомление за период, и те, на которые
        он откликнулся. Остальные вакансии полной подборки не отслеживаем — их сотни.
        """
        notified = select(VacancyEvent.vacancy_id).where(
            VacancyEvent.type == EventType.NEW, VacancyEvent.created_at >= matched_since
        )
        applied = select(VacancyMark.vacancy_id).where(VacancyMark.status == MarkStatus.APPLIED)
        result = await self._session.scalars(
            select(Vacancy)
            .where(
                Vacancy.status == VacancyStatus.OPEN,
                or_(Vacancy.id.in_(notified), Vacancy.id.in_(applied)),
            )
            .order_by(Vacancy.last_checked_at.asc().nulls_first(), Vacancy.id)
            .limit(limit)
        )
        return list(result)

    async def user_matches(self, user_id: int, vacancy_id: str) -> list[tuple[str, bool]]:
        """Подписки пользователя, под которые подошла вакансия: (название, is_backfill)."""
        result = await self._session.execute(
            select(Subscription.name, SubscriptionMatch.is_backfill)
            .join(Subscription, Subscription.id == SubscriptionMatch.subscription_id)
            .where(Subscription.user_id == user_id, SubscriptionMatch.vacancy_id == vacancy_id)
            .order_by(Subscription.id)
        )
        return [(name, is_backfill) for name, is_backfill in result.all()]

    async def mark_checked(
        self, vacancy_id: str, checked_at: datetime, status: VacancyStatus | None = None
    ) -> None:
        vacancy = await self._session.get_one(Vacancy, vacancy_id)
        vacancy.last_checked_at = checked_at
        if status is not None:
            vacancy.status = status

    async def apply_details(
        self, vacancy_id: str, details: VacancyDetails, checked_at: datetime
    ) -> None:
        """Обновить вакансию по свежим данным трекера.

        employer_id не трогаем: для вакансий из RSS там ключ по названию, на нём держится
        «Скрыть компанию».
        """
        vacancy = await self._session.get_one(Vacancy, vacancy_id)
        salary = details.salary or {}
        vacancy.name = details.name
        vacancy.salary_from = salary.get("from")
        vacancy.salary_to = salary.get("to")
        vacancy.salary_currency = salary.get("currency")
        vacancy.salary_gross = salary.get("gross")
        vacancy.status = VacancyStatus.ARCHIVED if details.archived else VacancyStatus.OPEN
        vacancy.last_checked_at = checked_at

    async def add_snapshot(self, vacancy_id: str, data: dict[str, Any]) -> VacancySnapshot:
        snapshot = VacancySnapshot(vacancy_id=vacancy_id, data=data)
        self._session.add(snapshot)
        await self._session.flush()
        return snapshot

    async def latest_snapshot(self, vacancy_id: str) -> VacancySnapshot | None:
        return await self._session.scalar(
            select(VacancySnapshot)
            .where(VacancySnapshot.vacancy_id == vacancy_id)
            .order_by(VacancySnapshot.taken_at.desc(), VacancySnapshot.id.desc())
            .limit(1)
        )
