"""Черновики откликов (часть 2): кнопки под карточкой и /drafts."""

from datetime import UTC, datetime, timedelta
from html import escape
from typing import Protocol

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.types import Message as TgMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.telegram.callbacks import DraftCB
from app.db.models import DraftStatus, MarkStatus, User, Vacancy
from app.db.repositories import DraftRepository
from app.services.catalog import mark

router = Router(name="drafts")

STATUS_NOTES = {
    "applied": "✅ Отмечено: откликнулся",
    "dismiss": "✖️ Пропущено",
    "rewrite": "🔁 Переписываю, новая карточка придёт через пару минут",
}

STATUS_TITLES = {
    DraftStatus.READY: "📝 Прислано черновиков",
    DraftStatus.APPLIED: "✅ Откликнулся",
    DraftStatus.DISMISSED: "✖️ Пропущено",
    DraftStatus.FILTERED: "🙈 Отсеяно моделью",
    DraftStatus.PENDING: "⏳ В очереди",
    DraftStatus.FAILED: "❌ Ошибки",
}


class DraftScheduler(Protocol):
    async def schedule_drafts(self) -> None: ...


async def _keep_only_link(callback: CallbackQuery, note: str) -> None:
    """Под карточкой остаётся только ссылка на вакансию, внизу — что сделали."""
    message = callback.message
    if not isinstance(message, TgMessage):
        return
    rows = []
    if message.reply_markup:
        rows = [[b for b in row if b.url] for row in message.reply_markup.inline_keyboard]
        rows = [row for row in rows if row]
    await message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await message.reply(note)


@router.callback_query(DraftCB.filter())
async def on_draft_action(
    callback: CallbackQuery,
    callback_data: DraftCB,
    session: AsyncSession,
    user: User,
    scheduler: DraftScheduler,
) -> None:
    repo = DraftRepository(session)
    draft = await repo.get_owned(user.id, callback_data.id)
    if draft is None:
        await callback.answer("Черновик не найден", show_alert=True)
        return
    vacancy = await session.get_one(Vacancy, draft.vacancy_id)
    match callback_data.action:
        case "applied":
            draft.status = DraftStatus.APPLIED
            # Та же отметка, что в подборке: вакансия не всплывёт там снова
            await mark(session, user.id, vacancy.id, MarkStatus.APPLIED)
        case "dismiss":
            draft.status = DraftStatus.DISMISSED
            await mark(session, user.id, vacancy.id, MarkStatus.SKIPPED)
        case "rewrite":
            draft.status = DraftStatus.PENDING
            draft.rewrites += 1
        case _:
            await callback.answer()
            return
    # Коммитим сразу: воркер должен увидеть PENDING, когда возьмёт задачу
    await session.commit()
    await callback.answer()
    if callback_data.action == "rewrite":
        await scheduler.schedule_drafts()
    await _keep_only_link(callback, STATUS_NOTES[callback_data.action])


@router.message(Command("drafts"))
async def cmd_drafts(message: TgMessage, session: AsyncSession, user: User) -> None:
    since = datetime.now(UTC) - timedelta(days=7)
    repo = DraftRepository(session)
    counts = await repo.counts(user.id, since)
    if not counts:
        await message.answer(
            "Черновиков за неделю нет. Они появляются для новых вакансий, "
            "если включено DRAFTS_ENABLED и запущена Ollama."
        )
        return
    lines = ["<b>Черновики за 7 дней</b>", ""]
    lines += [f"{title}: {counts[s]}" for s, title in STATUS_TITLES.items() if counts.get(s)]
    filtered = await repo.filtered(user.id, since, limit=8)
    if filtered:
        lines += ["", "<b>Последние отсеянные</b>"]
        for draft in filtered:
            vacancy = await session.get_one(Vacancy, draft.vacancy_id)
            reason = escape(draft.filter_reason or "")
            lines.append(f'• <a href="{escape(vacancy.url)}">{escape(vacancy.name)}</a>: {reason}')
    await message.answer("\n".join(lines))
