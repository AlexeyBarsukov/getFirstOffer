import pytest

from app.sources.dedup import company_name, dedup_key


@pytest.mark.parametrize(
    ("first", "second"),
    [
        # Грейд, скобки, дефис, порядок слов, регистр
        (
            ("Ромашка", "Frontend-разработчик (React)"),
            ("Ромашка", "Middle Frontend разработчик React"),
        ),
        (("Ромашка", "Разработчик Frontend"), ("Ромашка", "Frontend-разработчик")),
        (("ООО «Ромашка»", "Senior React developer"), ("Ромашка", "React Developer")),
        (("Ромашка", "Ведущий фронтенд-разработчик"), ("ромашка", "Фронтенд разработчик")),
        (("Ромашка", "Frontend (удалённо)"), ("Ромашка", "Frontend")),
    ],
)
def test_same_vacancy_same_key(first: tuple[str, str], second: tuple[str, str]) -> None:
    assert dedup_key(*first) == dedup_key(*second)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (("Ромашка", "Frontend-разработчик"), ("Лютик", "Frontend-разработчик")),
        (("Ромашка", "Frontend-разработчик"), ("Ромашка", "Backend-разработчик")),
        (("Ромашка", "React-разработчик"), ("Ромашка", "React Native разработчик")),
    ],
)
def test_different_vacancies_differ(first: tuple[str, str], second: tuple[str, str]) -> None:
    assert dedup_key(*first) != dedup_key(*second)


def test_nothing_to_compare() -> None:
    assert dedup_key("", "Frontend") is None
    assert dedup_key("Ромашка", "Senior (Middle)") is None


def test_company_name() -> None:
    assert company_name("АО «Тинькофф Банк»") == "тинькофф банк"
    assert company_name("Acme LLC") == "acme"
