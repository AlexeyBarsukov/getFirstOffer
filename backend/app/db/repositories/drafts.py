from datetime import datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApplicationDraft, DraftStatus


class DraftRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def queue_new_vacancies(self, user_id: int, since: datetime) -> int:
        """Черновики для новых вакансий пользователя (события NEW после since).

        Дубли с других площадок пропускаем: у первой вакансии группы черновик уже есть.
        """
        result = await self._session.execute(
            text(
                """
                INSERT INTO application_drafts (user_id, vacancy_id)
                SELECT e.user_id, e.vacancy_id
                FROM vacancy_events e
                JOIN vacancies v ON v.id = e.vacancy_id
                WHERE e.user_id = :user_id AND e.type = 'new' AND e.created_at >= :since
                  AND v.duplicate_of IS NULL AND v.status = 'open'
                ON CONFLICT (user_id, vacancy_id) DO NOTHING
                """
            ),
            {"user_id": user_id, "since": since},
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def next_pending(self, user_id: int, skip: set[int]) -> ApplicationDraft | None:
        """Самый свежий ожидающий: на свежую вакансию важнее откликнуться быстро."""
        stmt = (
            select(ApplicationDraft)
            .where(
                ApplicationDraft.user_id == user_id,
                ApplicationDraft.status == DraftStatus.PENDING,
            )
            .order_by(ApplicationDraft.created_at.desc(), ApplicationDraft.id.desc())
            .limit(1)
        )
        if skip:
            stmt = stmt.where(ApplicationDraft.id.not_in(skip))
        result: ApplicationDraft | None = await self._session.scalar(stmt)
        return result

    async def recent_letters(self, user_id: int, limit: int = 20) -> list[str]:
        result = await self._session.scalars(
            select(ApplicationDraft.letter)
            .where(ApplicationDraft.user_id == user_id, ApplicationDraft.letter.is_not(None))
            .order_by(ApplicationDraft.updated_at.desc())
            .limit(limit)
        )
        return [letter for letter in result if letter]

    async def get_owned(self, user_id: int, draft_id: int) -> ApplicationDraft | None:
        draft = await self._session.get(ApplicationDraft, draft_id)
        return draft if draft is not None and draft.user_id == user_id else None

    async def set_status(self, draft_id: int, status: DraftStatus) -> None:
        await self._session.execute(
            update(ApplicationDraft).where(ApplicationDraft.id == draft_id).values(status=status)
        )

    async def counts(self, user_id: int, since: datetime) -> dict[DraftStatus, int]:
        rows = await self._session.execute(
            select(ApplicationDraft.status, func.count())
            .where(ApplicationDraft.user_id == user_id, ApplicationDraft.updated_at >= since)
            .group_by(ApplicationDraft.status)
        )
        return {DraftStatus(status): count for status, count in rows}

    async def filtered(
        self, user_id: int, since: datetime, limit: int = 10
    ) -> list[ApplicationDraft]:
        result = await self._session.scalars(
            select(ApplicationDraft)
            .where(
                ApplicationDraft.user_id == user_id,
                ApplicationDraft.status == DraftStatus.FILTERED,
                ApplicationDraft.updated_at >= since,
            )
            .order_by(ApplicationDraft.updated_at.desc())
            .limit(limit)
        )
        return list(result)
