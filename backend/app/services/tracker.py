"""Отслеживание изменений уже найденных вакансий: архив, удаление, зарплата, условия.

Одна проверка: текущее состояние → сравнение с последним снимком → события тем, кому
мы уже присылали эту вакансию → доставки. Транзакцией управляет воркер.
"""

from datetime import datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EventType, VacancyStatus
from app.db.repositories import EventRepository, UserRepository, VacancyRepository
from app.services.presets import EXPERIENCE_NAMES, WORK_FORMAT_NAMES
from app.sources.details import VacancyDetailsSource

logger = structlog.get_logger(__name__)

# Поля для события «Вакансия изменилась» (зарплата и архив — отдельные события)
CHANGED_FIELDS = ("name", "experience", "work_format", "area")

Change = tuple[EventType, dict[str, Any]]


def _humanize(field: str, value: Any) -> str:
    if value in (None, [], ""):
        return "—"
    if field == "experience":
        return EXPERIENCE_NAMES.get(str(value), str(value))
    if field == "work_format":
        return ", ".join(WORK_FORMAT_NAMES.get(v, v) for v in value)
    return str(value)


def diff_snapshots(old: dict[str, Any], new: dict[str, Any]) -> list[Change]:
    if old.get("source") != new.get("source"):
        # API и страница сайта описывают поля чуть по-разному — сравнение дало бы ложные изменения
        return []
    if new["archived"] and not old["archived"]:
        # Остальные изменения в архивной вакансии уже не интересны
        return [(EventType.ARCHIVED, {})]

    changes: list[Change] = []
    if old.get("salary") != new.get("salary"):
        changes.append(
            (
                EventType.SALARY_CHANGED,
                {"salary": {"old": old.get("salary"), "new": new.get("salary")}},
            )
        )
    changed = {
        field: {"old": _humanize(field, old.get(field)), "new": _humanize(field, new.get(field))}
        for field in CHANGED_FIELDS
        if old.get(field) != new.get(field)
    }
    if changed:
        changes.append((EventType.CHANGED, changed))
    return changes


async def track_vacancy(
    session: AsyncSession, source: VacancyDetailsSource, vacancy_id: str, now: datetime
) -> int:
    """Проверяет одну вакансию. Возвращает число созданных событий."""
    vacancies = VacancyRepository(session)
    details = await source.get_details(vacancy_id)

    if details is None:
        await vacancies.mark_checked(vacancy_id, now, VacancyStatus.CLOSED)
        changes: list[Change] = [(EventType.CLOSED, {})]
    else:
        snapshot = details.snapshot(source.name)
        previous = await vacancies.latest_snapshot(vacancy_id)
        # Первый снимок — точка отсчёта, событий нет
        changes = diff_snapshots(previous.data, snapshot) if previous else []
        if previous is None or previous.data != snapshot:
            await vacancies.add_snapshot(vacancy_id, snapshot)
        await vacancies.apply_details(vacancy_id, details, now)

    if not changes:
        return 0

    events = EventRepository(session)
    users = UserRepository(session)
    created = 0
    for user_id in await events.users_notified_about(vacancy_id):
        channel_ids = [c.id for c in await users.active_channels(user_id)]
        for type_, payload in changes:
            event = await events.add_event(user_id, vacancy_id, type_, payload)
            await events.add_deliveries(event.id, channel_ids)
            created += 1
    logger.info(
        "vacancy_changed",
        vacancy_id=vacancy_id,
        changes=[t.value for t, _ in changes],
        events=created,
        source=source.name,
    )
    return created
