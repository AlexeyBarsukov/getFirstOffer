"""Тексты уведомлений.

Формат payload событий об изменениях (заполняет трекер на этапе 8):
    salary_changed: {"salary": {"old": SalaryDict | None, "new": SalaryDict | None}}
    changed:        {"<поле>": {"old": ..., "new": ...}, ...}
где SalaryDict = {"from": int | None, "to": int | None, "currency": str | None,
                  "gross": bool | None}.
"""

from collections.abc import Sequence
from datetime import datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from app.bots.telegram.callbacks import HideEmployerCB
from app.db.models import EventType, Vacancy, VacancyEvent
from app.notifiers.base import Button, Message
from app.sources.employers import employer_key
from app.sources.portals import PORTAL_TITLES, PORTAL_TITLES_PREPOSITIONAL, portal_of

MOSCOW = ZoneInfo("Europe/Moscow")
NBSP = " "

CURRENCY_SIGNS = {"RUR": "₽", "RUB": "₽", "USD": "$", "EUR": "€", "KZT": "₸", "BYR": "Br"}

CHANGED_FIELD_LABELS = {
    "name": "Название",
    "schedule": "График",
    "employment": "Занятость",
    "experience": "Опыт",
    "work_format": "Формат работы",
    "address": "Адрес",
    "area": "Город",
}


def format_number(value: int) -> str:
    return f"{value:,}".replace(",", NBSP)


def format_salary(
    salary_from: int | None,
    salary_to: int | None,
    currency: str | None,
    gross: bool | None,
) -> str:
    if salary_from is None and salary_to is None:
        return "не указана"
    sign = CURRENCY_SIGNS.get(currency or "", currency or "")
    if salary_from is not None and salary_to is not None:
        amount = f"{format_number(salary_from)} – {format_number(salary_to)}"
    elif salary_from is not None:
        amount = f"от {format_number(salary_from)}"
    else:
        amount = f"до {format_number(salary_to or 0)}"
    text = f"{amount}{NBSP}{sign}".rstrip()
    if gross is True:
        text += " до вычета налогов"
    elif gross is False:
        text += " на руки"
    return text


def format_salary_dict(salary: dict[str, Any] | None) -> str:
    if not salary:
        return "не указана"
    return format_salary(
        salary.get("from"), salary.get("to"), salary.get("currency"), salary.get("gross")
    )


def format_time(moment: datetime) -> str:
    return moment.astimezone(MOSCOW).strftime("%d.%m %H:%M") + " МСК"


def _raw_name(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if isinstance(value, dict) and value.get("name"):
        return str(value["name"])
    return None


def _work_formats(raw: dict[str, Any]) -> str | None:
    items = raw.get("work_format") or []
    names = [str(i["name"]) for i in items if isinstance(i, dict) and i.get("name")]
    return ", ".join(names) or None


def vacancy_lines(vacancy: Vacancy) -> list[str]:
    salary = format_salary(
        vacancy.salary_from, vacancy.salary_to, vacancy.salary_currency, vacancy.salary_gross
    )
    place = " · ".join(p for p in [vacancy.area, _work_formats(vacancy.raw)] if p)
    lines = [
        f"<b>{escape(vacancy.name)}</b>",
        f"🏢 {escape(vacancy.employer_name)}",
        f"💰 {escape(salary)}",
        f"📍 {escape(place)}",
    ]
    if experience := _raw_name(vacancy.raw, "experience"):
        lines.append(f"🎓 {escape(experience)}")
    return lines


def published_line(vacancy: Vacancy) -> str:
    portal = PORTAL_TITLES[portal_of(vacancy.id)]
    return f"🕒 Опубликована {format_time(vacancy.published_at)} · {portal}"


def _new_vacancy_header(matches: Sequence[tuple[str, bool]]) -> str:
    # Вакансия попала только в подгрузку за 24 часа — помечаем, чтобы не путать со свежими
    if matches and all(is_backfill for _, is_backfill in matches):
        return "📦 <i>За последние 24 часа</i>"
    return "🆕 <b>Новая вакансия</b>"


def _change_lines(event: VacancyEvent, vacancy: Vacancy) -> list[str]:
    match event.type:
        case EventType.ARCHIVED:
            return ["🗄 <b>Вакансия перенесена в архив</b>"]
        case EventType.CLOSED:
            where = PORTAL_TITLES_PREPOSITIONAL[portal_of(vacancy.id)]
            return [f"❌ <b>Вакансия снята с публикации на {where}</b>"]
        case EventType.SALARY_CHANGED:
            change = event.payload.get("salary", {})
            old = format_salary_dict(change.get("old"))
            new = format_salary_dict(change.get("new"))
            return [f"💰 <b>Изменилась зарплата:</b> {escape(old)} → {escape(new)}"]
        case _:
            lines = ["✏️ <b>Вакансия изменилась</b>"]
            for field_name, change in event.payload.items():
                label = CHANGED_FIELD_LABELS.get(field_name, field_name)
                old, new = change.get("old"), change.get("new")
                lines.append(f"• {escape(label)}: {escape(str(old))} → {escape(str(new))}")
            return lines


def build_message(
    event: VacancyEvent, vacancy: Vacancy, matches: Sequence[tuple[str, bool]]
) -> Message:
    """matches — подписки пользователя, под которые подошла вакансия: (название, is_backfill)."""
    is_new = event.type == EventType.NEW
    header = [_new_vacancy_header(matches)] if is_new else _change_lines(event, vacancy)

    lines = [*header, "", *vacancy_lines(vacancy)]
    if matches:
        names = ", ".join(escape(name) for name, _ in matches)
        lines.append(f"🔎 {names}")
    lines.append(published_line(vacancy))

    buttons = [
        Button(f"Открыть на {PORTAL_TITLES_PREPOSITIONAL[portal_of(vacancy.id)]}", url=vacancy.url)
    ]
    # У анонимного работодателя id нет — кнопку не показываем, иначе скрылись бы все анонимные.
    # Скрываем по ключу из названия: он одинаковый для API, RSS и страницы поиска
    if vacancy.employer_id:
        buttons.append(hide_employer_button(employer_key(vacancy.employer_name), undo=False))
    return Message(text="\n".join(lines), buttons=buttons)


def hide_employer_button(employer_id: str, *, undo: bool) -> Button:
    text = "↩️ Снова показывать компанию" if undo else "🚫 Скрыть компанию"
    return Button(text, callback_data=HideEmployerCB(employer_id=employer_id, undo=undo).pack())
