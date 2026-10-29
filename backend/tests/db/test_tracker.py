from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    ChannelType,
    Delivery,
    EventType,
    SourceName,
    Vacancy,
    VacancyEvent,
    VacancyStatus,
)
from app.db.repositories import (
    EventRepository,
    SubscriptionRepository,
    UserRepository,
    VacancyRepository,
)
from app.services.tracker import diff_snapshots, track_vacancy
from app.sources.base import SearchFilters
from app.sources.details import VacancyDetails
from app.workers.tracker import track_vacancies
from tests.factories import FakeAlerter, FakeRedis, make_vacancy, portals

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def details(**overrides: Any) -> VacancyDetails:
    fields: dict[str, Any] = {
        "id": "1",
        "name": "Frontend-разработчик",
        "archived": False,
        "salary": {"from": 80000, "to": None, "currency": "RUR", "gross": False},
        "experience": "noExperience",
        "work_format": ["REMOTE"],
        "area": "Москва",
    }
    fields.update(overrides)
    return VacancyDetails(**fields)


class FakeDetailsSource:
    def __init__(self, state: dict[str, VacancyDetails | Exception | None], name: str = "page"):
        self.state = state
        self.name = name

    async def get_details(self, vacancy_id: str, url: str | None = None) -> VacancyDetails | None:
        value = self.state[vacancy_id]
        if isinstance(value, Exception):
            raise value
        return value


async def notified_user(session: AsyncSession, chat_id: str, *vacancy_ids: str) -> tuple[int, int]:
    """Пользователь, которому уже пришли эти вакансии."""
    user, channel = await UserRepository(session).get_or_create_by_channel(
        ChannelType.TELEGRAM, chat_id
    )
    subscription = await SubscriptionRepository(session).create(
        user.id, "Frontend", SearchFilters(text="frontend")
    )
    vacancies = VacancyRepository(session)
    await vacancies.upsert_many(
        [make_vacancy(v, published_at=NOW) for v in vacancy_ids], SourceName.RSS
    )
    await vacancies.add_matches(subscription.id, list(vacancy_ids))
    await EventRepository(session).add_new_vacancy_events(user.id, list(vacancy_ids))
    return user.id, channel.id


async def change_events(session: AsyncSession) -> list[VacancyEvent]:
    result = await session.scalars(
        select(VacancyEvent).where(VacancyEvent.type != EventType.NEW).order_by(VacancyEvent.id)
    )
    return list(result)


def test_diff_ignores_snapshots_from_different_sources() -> None:
    old = details(salary=None).snapshot("api")
    new = details().snapshot("page")

    assert diff_snapshots(old, new) == []


def test_diff_archived_supersedes_other_changes() -> None:
    old = details().snapshot("page")
    new = details(archived=True, name="Другое").snapshot("page")

    assert diff_snapshots(old, new) == [(EventType.ARCHIVED, {})]


def test_diff_salary_and_fields() -> None:
    old = details().snapshot("page")
    new = details(
        salary={"from": 100000, "to": None, "currency": "RUR", "gross": False},
        work_format=["REMOTE", "HYBRID"],
        experience="between1And3",
    ).snapshot("page")

    assert diff_snapshots(old, new) == [
        (
            EventType.SALARY_CHANGED,
            {
                "salary": {
                    "old": {"from": 80000, "to": None, "currency": "RUR", "gross": False},
                    "new": {"from": 100000, "to": None, "currency": "RUR", "gross": False},
                }
            },
        ),
        (
            EventType.CHANGED,
            {
                "experience": {"old": "Без опыта", "new": "1–3 года"},
                "work_format": {"old": "Удалённо", "new": "Гибрид, Удалённо"},
            },
        ),
    ]


async def test_first_check_is_baseline_then_salary_change_is_notified(
    session: AsyncSession,
) -> None:
    _, channel_id = await notified_user(session, "1001", "1")
    source = FakeDetailsSource({"1": details()})

    assert await track_vacancy(session, source, "1", NOW) == 0

    source.state["1"] = details(
        salary={"from": 120000, "to": None, "currency": "RUR", "gross": False}
    )
    assert await track_vacancy(session, source, "1", NOW + timedelta(minutes=30)) == 1

    [event] = await change_events(session)
    assert event.type == EventType.SALARY_CHANGED
    assert event.payload["salary"]["new"]["from"] == 120000
    delivery = await session.get_one(Delivery, (event.id, channel_id))
    assert delivery is not None
    vacancy = await session.get_one(Vacancy, "1")
    assert vacancy.salary_from == 120000
    assert vacancy.last_checked_at == NOW + timedelta(minutes=30)


async def test_unchanged_vacancy_creates_no_events_or_snapshots(session: AsyncSession) -> None:
    await notified_user(session, "1001", "1")
    source = FakeDetailsSource({"1": details()})
    vacancies = VacancyRepository(session)

    await track_vacancy(session, source, "1", NOW)
    first = await vacancies.latest_snapshot("1")
    await track_vacancy(session, source, "1", NOW + timedelta(minutes=30))

    assert await change_events(session) == []
    latest = await vacancies.latest_snapshot("1")
    assert first is not None and latest is not None
    assert latest.id == first.id


async def test_archived_vacancy_leaves_tracking_queue(session: AsyncSession) -> None:
    await notified_user(session, "1001", "1")
    source = FakeDetailsSource({"1": details()})
    await track_vacancy(session, source, "1", NOW)

    source.state["1"] = details(archived=True)
    await track_vacancy(session, source, "1", NOW)

    [event] = await change_events(session)
    assert event.type == EventType.ARCHIVED
    vacancy = await session.get_one(Vacancy, "1")
    assert vacancy.status == VacancyStatus.ARCHIVED
    assert await VacancyRepository(session).list_for_tracking(NOW - timedelta(days=30), 10) == []


async def test_deleted_vacancy_is_closed(session: AsyncSession) -> None:
    await notified_user(session, "1001", "1")

    await track_vacancy(session, FakeDetailsSource({"1": None}), "1", NOW)

    [event] = await change_events(session)
    assert event.type == EventType.CLOSED
    assert (await session.get_one(Vacancy, "1")).status == VacancyStatus.CLOSED


async def test_only_users_who_got_the_vacancy_are_notified(session: AsyncSession) -> None:
    notified_id, _ = await notified_user(session, "1001", "1")
    # Второй пользователь видел вакансию в подписке, но уведомления о ней не получал
    other, _ = await UserRepository(session).get_or_create_by_channel(ChannelType.TELEGRAM, "2")
    subscription = await SubscriptionRepository(session).create(
        other.id, "IT", SearchFilters(text="it")
    )
    await VacancyRepository(session).add_matches(subscription.id, ["1"])

    await track_vacancy(session, FakeDetailsSource({"1": None}), "1", NOW)

    assert [e.user_id for e in await change_events(session)] == [notified_id]


async def test_worker_continues_after_failure_and_triggers_notifications(
    session: AsyncSession,
) -> None:
    await notified_user(session, "1001", "ok", "broken")
    await session.commit()
    redis = FakeRedis()
    ctx: dict[str, Any] = {
        "redis": redis,
        "alerter": FakeAlerter(),
        "session_factory": async_sessionmaker(
            bind=session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ),
        "portals": portals(details=FakeDetailsSource({"ok": None, "broken": RuntimeError("403")})),
    }

    summary = await track_vacancies(ctx)

    assert summary == {"checked": 1, "failed": 1, "events": 1}
    assert [name for name, _ in redis.enqueued] == ["send_notifications"]
    assert redis.held == set()
