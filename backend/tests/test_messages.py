from datetime import UTC, datetime
from typing import Any

import pytest

from app.db.models import EventType, SourceName, Vacancy, VacancyEvent
from app.notifiers.messages import build_message, format_salary
from app.sources.employers import employer_key

PUBLISHED = datetime(2026, 10, 25, 9, 1, tzinfo=UTC)  # 12:01 МСК


def make_db_vacancy(**overrides: Any) -> Vacancy:
    fields: dict[str, Any] = {
        "id": "110000001",
        "name": "Junior Frontend <React>",
        "employer_id": "1740",
        "employer_name": "Яндекс & Co",
        "area": "Москва",
        "salary_from": 80000,
        "salary_to": 120000,
        "salary_currency": "RUR",
        "salary_gross": False,
        "url": "https://hh.ru/vacancy/110000001",
        "published_at": PUBLISHED,
        "first_seen_source": SourceName.API,
        "raw": {
            "experience": {"id": "noExperience", "name": "Нет опыта"},
            "work_format": [{"id": "REMOTE", "name": "Удалённо"}],
        },
    }
    fields.update(overrides)
    return Vacancy(**fields)


def make_event(type_: EventType, payload: dict[str, Any] | None = None) -> VacancyEvent:
    return VacancyEvent(user_id=1, vacancy_id="110000001", type=type_, payload=payload or {})


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((80000, 120000, "RUR", False), "80 000 – 120 000 ₽ на руки"),
        ((80000, None, "RUR", True), "от 80 000 ₽ до вычета налогов"),
        ((None, 3000, "USD", None), "до 3 000 $"),
        ((None, None, None, None), "не указана"),
        ((1000, None, "GEL", None), "от 1 000 GEL"),
    ],
)
def test_format_salary(args: tuple[Any, ...], expected: str) -> None:
    assert format_salary(*args) == expected


def test_new_vacancy_card() -> None:
    message = build_message(
        make_event(EventType.NEW), make_db_vacancy(), [("Frontend", False), ("IT", True)]
    )

    assert message.text.splitlines() == [
        "🆕 <b>Новая вакансия</b>",
        "",
        "<b>Junior Frontend &lt;React&gt;</b>",
        "🏢 Яндекс &amp; Co",
        "💰 80 000 – 120 000 ₽ на руки",
        "📍 Москва · Удалённо",
        "🎓 Нет опыта",
        "🔎 Frontend, IT",
        "🕒 Опубликована 25.10 12:01 МСК · hh.ru",
    ]
    assert [(b.text, b.url, b.callback_data) for b in message.buttons] == [
        ("Открыть на hh.ru", "https://hh.ru/vacancy/110000001", None),
        ("🚫 Скрыть компанию", None, f"hide:{employer_key('Яндекс & Co')}:0"),
    ]


def test_anonymous_employer_has_no_hide_button() -> None:
    message = build_message(make_event(EventType.NEW), make_db_vacancy(employer_id=None), [])

    assert [b.text for b in message.buttons] == ["Открыть на hh.ru"]


def test_backfill_only_vacancy_is_marked() -> None:
    message = build_message(make_event(EventType.NEW), make_db_vacancy(), [("Frontend", True)])

    assert message.text.startswith("📦 <i>За последние 24 часа</i>")


def test_card_without_optional_fields() -> None:
    vacancy = make_db_vacancy(salary_from=None, salary_to=None, raw={})

    text = build_message(make_event(EventType.NEW), vacancy, []).text

    assert "💰 не указана" in text
    assert "📍 Москва" in text
    assert "🎓" not in text
    assert "🔎" not in text


def test_salary_changed() -> None:
    event = make_event(
        EventType.SALARY_CHANGED,
        {
            "salary": {
                "old": {"from": 80000, "to": None, "currency": "RUR", "gross": False},
                "new": {"from": 100000, "to": None, "currency": "RUR", "gross": False},
            }
        },
    )

    first_line = build_message(event, make_db_vacancy(), []).text.splitlines()[0]

    assert first_line == (
        "💰 <b>Изменилась зарплата:</b> от 80 000 ₽ на руки → от 100 000 ₽ на руки"
    )


@pytest.mark.parametrize(
    ("type_", "header"),
    [
        (EventType.ARCHIVED, "🗄 <b>Вакансия перенесена в архив</b>"),
        (EventType.CLOSED, "❌ <b>Вакансия снята с публикации на hh.ru</b>"),
    ],
)
def test_status_change_headers(type_: EventType, header: str) -> None:
    assert build_message(make_event(type_), make_db_vacancy(), []).text.startswith(header)


def test_generic_change_lists_fields() -> None:
    event = make_event(EventType.CHANGED, {"schedule": {"old": "Полный день", "new": "Удалённо"}})

    lines = build_message(event, make_db_vacancy(), []).text.splitlines()

    assert lines[:2] == ["✏️ <b>Вакансия изменилась</b>", "• График: Полный день → Удалённо"]
