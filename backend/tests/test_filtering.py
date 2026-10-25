from datetime import UTC, datetime

import pytest

from app.services.filtering import find_excluded_word, is_excluded
from app.services.presets import DEFAULT_EXCLUDE_WORDS
from app.sources.employers import employer_key
from tests.factories import make_vacancy

NOW = datetime(2026, 10, 25, tzinfo=UTC)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Senior Frontend Developer", "senior"),
        ("Team Lead frontend", "lead"),
        ("Lead-разработчик React", "lead"),
        ("Ведущий фронтенд-разработчик", "ведущий"),
        ("Программист 1С", "1с"),
        ("Программист 1C (латиница)", "1c"),
        ("Руководитель группы frontend", "руководитель"),
        ("Junior Frontend Developer", None),
        ("Frontend developer, leading fintech company", None),
        ("Middle React разработчик", None),
    ],
)
def test_find_excluded_word(title: str, expected: str | None) -> None:
    assert find_excluded_word(title, DEFAULT_EXCLUDE_WORDS) == expected


def test_empty_words_are_ignored() -> None:
    assert find_excluded_word("Frontend", ["", "  "]) is None


def test_is_excluded_by_employer() -> None:
    vacancy = make_vacancy("1", "Junior Frontend", published_at=NOW, employer_id="1740")

    assert is_excluded(vacancy, [], ["1740"]) is True
    assert is_excluded(vacancy, [], ["3529"]) is False


def test_anonymous_employer_is_not_excluded_by_employer_list() -> None:
    vacancy = make_vacancy("1", "Junior Frontend", published_at=NOW, employer_id=None)

    assert is_excluded(vacancy, [], ["1740"]) is False


def test_is_excluded_by_name_key_from_any_source() -> None:
    # «Скрыть компанию» сохраняет ключ по названию — он срабатывает и для вакансий из API,
    # где у компании настоящий id
    vacancy = make_vacancy("1", "Junior Frontend", published_at=NOW, employer_id="1740")

    assert is_excluded(vacancy, [], [employer_key(vacancy.employer.name)]) is True
