"""Карточка черновика отклика в Telegram."""

from html import escape

from app.apply.profile import ROLE_TITLES, Role
from app.bots.telegram.callbacks import DraftCB
from app.db.models import ApplicationDraft, Vacancy
from app.notifiers.base import Button, Message
from app.notifiers.messages import published_line, vacancy_lines
from app.sources.portals import PORTAL_TITLES_PREPOSITIONAL, portal_of


def draft_warnings(draft: ApplicationDraft) -> list[str]:
    lines = []
    if draft.external_url:
        lines.append("🔗 Отклик на сайте компании")
    if draft.has_test:
        lines.append("🧪 У вакансии есть тест")
    if draft.letter_required:
        lines.append("✉️ Письмо обязательно")
    if draft.required_phrase:
        lines.append(f"🔑 Просят указать в письме: «{escape(draft.required_phrase)}»")
    if draft.problems:
        lines.append("⚠️ Не исправлено: " + escape("; ".join(draft.problems)))
    return lines


def build_draft_message(draft: ApplicationDraft, vacancy: Vacancy) -> Message:
    role = ROLE_TITLES.get(Role(draft.role), draft.role) if draft.role else "?"
    lines = [
        f"📝 <b>Черновик отклика</b> · {escape(role)} · {draft.score}/100",
        "",
        *vacancy_lines(vacancy),
        published_line(vacancy),
    ]
    if draft.reasons:
        lines += ["", "<i>" + escape("; ".join(draft.reasons)) + "</i>"]
    if warnings := draft_warnings(draft):
        lines += ["", *warnings]
    # <pre>: в Telegram у блока есть кнопка «Копировать»
    lines += ["", f"<pre>{escape(draft.letter or '')}</pre>"]
    where = PORTAL_TITLES_PREPOSITIONAL[portal_of(vacancy.id)]
    return Message(
        text="\n".join(lines),
        buttons=[
            Button(f"↗️ Открыть на {where}", url=draft.external_url or vacancy.url),
            Button("✅ Откликнулся", callback_data=DraftCB(action="applied", id=draft.id).pack()),
            Button("🔁 Переписать", callback_data=DraftCB(action="rewrite", id=draft.id).pack()),
            Button("✖️ Пропустить", callback_data=DraftCB(action="dismiss", id=draft.id).pack()),
        ],
    )
