"""Задача arq: черновики откликов для новых вакансий (часть 2, режим «черновики»).

Раз в минуту: ставит в очередь черновики для новых вакансий и пишет их по одному, самые свежие
первыми. Модель на процессоре пишет письмо минуты, поэтому за запуск обрабатываем столько,
сколько успеем за DRAFTS_TIME_BUDGET; остальное — в следующих запусках.

ctx: redis, session_factory, notifiers, drafts (DraftsContext | None).
"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select

from app.apply.drafter import Drafter
from app.apply.llm import LLM, LLMError
from app.apply.messages import build_draft_message
from app.apply.profile import CandidateProfile
from app.apply.service import process_draft
from app.apply.vacancy_text import VacancyTextSource
from app.db.models import ApplicationDraft, ChannelType, DraftStatus, UserChannel, Vacancy
from app.db.repositories import DraftRepository
from app.workers.locks import release

logger = structlog.get_logger(__name__)

DRAFTS_LOCK_KEY = "drafts:cycle"
# Задача живёт до 15 минут; новые черновики берём, пока не прошло 10
DRAFTS_TIME_BUDGET = 10 * 60
DRAFTS_LOCK_TTL = 16 * 60
# Черновики только для вакансий, найденных недавно: после долгого простоя не разбираем завал
DRAFTS_WINDOW = timedelta(hours=24)
DRAFT_NOW_JOB_ID = "prepare-drafts-now"


@dataclass
class DraftsContext:
    llm: LLM
    text_source: VacancyTextSource
    profile: CandidateProfile
    chat_id: int
    min_score: int
    temperature: float = 0.7

    @property
    def drafter(self) -> Drafter:
        return Drafter(self.llm, self.profile, self.temperature)

    async def aclose(self) -> None:
        await self.llm.aclose()
        await self.text_source.aclose()


async def _channel(ctx: dict[str, Any], chat_id: int) -> UserChannel | None:
    async with ctx["session_factory"]() as session:
        channel: UserChannel | None = await session.scalar(
            select(UserChannel).where(
                UserChannel.type == ChannelType.TELEGRAM, UserChannel.external_id == str(chat_id)
            )
        )
        return channel


async def _send_card(ctx: dict[str, Any], channel: UserChannel, draft_id: int) -> None:
    notifier = ctx["notifiers"].get(ChannelType.TELEGRAM)
    if notifier is None:
        return
    async with ctx["session_factory"]() as session:
        draft = await session.get_one(ApplicationDraft, draft_id)
        vacancy = await session.get_one(Vacancy, draft.vacancy_id)
        await notifier.send(channel.external_id, build_draft_message(draft, vacancy))
        draft.sent_at = datetime.now(UTC)
        await session.commit()


async def _resend_unsent(ctx: dict[str, Any], channel: UserChannel) -> None:
    """Карточки, которые не ушли в прошлый раз (Telegram был недоступен)."""
    async with ctx["session_factory"]() as session:
        ids = list(
            await session.scalars(
                select(ApplicationDraft.id).where(
                    ApplicationDraft.user_id == channel.user_id,
                    ApplicationDraft.status == DraftStatus.READY,
                    ApplicationDraft.sent_at.is_(None),
                )
            )
        )
    for draft_id in ids:
        try:
            await _send_card(ctx, channel, draft_id)
        except Exception:
            logger.exception("draft_card_send_failed", draft_id=draft_id)
            return


async def prepare_drafts(ctx: dict[str, Any]) -> dict[str, int]:
    drafts: DraftsContext | None = ctx.get("drafts")
    if drafts is None:
        return {}
    lock = ctx["redis"].lock(DRAFTS_LOCK_KEY, timeout=DRAFTS_LOCK_TTL)
    if not await lock.acquire(blocking=False):
        return {}
    stats: dict[str, int] = {}
    try:
        channel = await _channel(ctx, drafts.chat_id)
        if channel is None:
            logger.warning("drafts_no_channel", chat_id=drafts.chat_id, hint="напиши боту /start")
            return {}
        async with ctx["session_factory"]() as session:
            queued = await DraftRepository(session).queue_new_vacancies(
                channel.user_id, datetime.now(UTC) - DRAFTS_WINDOW
            )
            await session.commit()
        if queued:
            stats["queued"] = queued
        await _resend_unsent(ctx, channel)
        started = time.monotonic()
        failed: set[int] = set()
        drafter = drafts.drafter
        while time.monotonic() - started < DRAFTS_TIME_BUDGET:
            async with ctx["session_factory"]() as session:
                draft = await DraftRepository(session).next_pending(channel.user_id, failed)
                if draft is None:
                    break
                draft_id = draft.id
                try:
                    status = await process_draft(
                        session,
                        draft,
                        drafts.text_source,
                        drafter,
                        drafts.profile,
                        drafts.min_score,
                    )
                except LLMError as exc:
                    # Ollama не запущена или упала — остальные черновики ждут следующего запуска
                    logger.warning("drafts_llm_unavailable", error=str(exc))
                    await session.rollback()
                    break
                except Exception as exc:
                    logger.exception("draft_failed", draft_id=draft_id)
                    await session.rollback()
                    failed.add(draft_id)
                    draft = await session.get_one(ApplicationDraft, draft_id)
                    draft.status = DraftStatus.FAILED
                    draft.error = repr(exc)[:1000]
                    await session.commit()
                    stats["failed"] = stats.get("failed", 0) + 1
                    continue
                await session.commit()
            stats[status.value] = stats.get(status.value, 0) + 1
            if status == DraftStatus.READY:
                try:
                    await _send_card(ctx, channel, draft_id)
                except Exception:
                    logger.exception("draft_card_send_failed", draft_id=draft_id)
        if stats:
            logger.info("drafts_done", **stats)
        return stats
    finally:
        await release(lock)


async def enqueue_drafts_now(redis: Any) -> None:
    await redis.enqueue_job("prepare_drafts", _job_id=DRAFT_NOW_JOB_ID)
