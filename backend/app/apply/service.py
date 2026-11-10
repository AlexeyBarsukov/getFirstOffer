"""Один черновик: текст вакансии → модель → решение → запись в БД. Commit делает воркер."""

from html import unescape

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.apply.decision import decide
from app.apply.drafter import Drafter, VacancyBrief
from app.apply.profile import CandidateProfile
from app.apply.vacancy_text import VacancyTextSource
from app.db.models import ApplicationDraft, DraftStatus, Vacancy
from app.db.repositories import DraftRepository
from app.notifiers.messages import format_salary, work_formats

logger = structlog.get_logger(__name__)


def vacancy_brief(vacancy: Vacancy) -> VacancyBrief:
    return VacancyBrief(
        name=unescape(vacancy.name),
        employer=vacancy.employer_name,
        area=vacancy.area,
        salary=format_salary(
            vacancy.salary_from, vacancy.salary_to, vacancy.salary_currency, vacancy.salary_gross
        ),
        work_format=work_formats(vacancy.raw) or "",
    )


async def process_draft(
    session: AsyncSession,
    draft: ApplicationDraft,
    text_source: VacancyTextSource,
    drafter: Drafter,
    profile: CandidateProfile,
    min_score: int,
) -> DraftStatus:
    """Заполняет черновик. READY — карточку нужно отправить."""
    vacancy = await session.get_one(Vacancy, draft.vacancy_id)
    text = await text_source.get_text(vacancy.id, vacancy.url, vacancy.raw)
    recent = await DraftRepository(session).recent_letters(draft.user_id)
    # Переписываем по просьбе: старое письмо не должно повториться
    if draft.letter:
        recent.insert(0, draft.letter)
    result = await drafter.draft(vacancy_brief(vacancy), text, recent)
    decision = decide(
        profile,
        result.role,
        result.score,
        min_score,
        vacancy.salary_from,
        vacancy.salary_to,
        vacancy.salary_currency,
    )
    draft.role = result.role.value
    draft.score = result.score
    draft.reasons = result.reasons
    draft.letter = result.letter
    draft.problems = result.problems
    draft.required_phrase = result.required_phrase.text if result.required_phrase else None
    draft.letter_required = text.letter_required
    draft.has_test = text.has_test
    draft.external_url = text.external_url
    draft.model = result.model
    draft.attempts = result.attempts
    draft.seconds = result.seconds
    draft.error = None
    draft.filter_reason = decision.reason
    # Переписать просили вручную — карточку шлём, даже если модель передумала с оценкой
    draft.status = DraftStatus.READY if decision.send or draft.rewrites else DraftStatus.FILTERED
    await session.flush()
    return draft.status
