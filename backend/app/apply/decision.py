"""Что делать с вакансией после анализа: прислать черновик или отсеять."""

from dataclasses import dataclass

from app.apply.profile import CandidateProfile, Role

# Валюты, в которых сравниваем зарплату с порогом роли
RUBLES = {"RUR", "RUB", None}


@dataclass(frozen=True)
class Decision:
    send: bool
    reason: str | None = None


def decide(
    profile: CandidateProfile,
    role: Role,
    score: int,
    min_score: int,
    salary_from: int | None,
    salary_to: int | None,
    currency: str | None,
) -> Decision:
    if role == Role.OTHER:
        return Decision(False, "роль не из списка (frontend, QA, поддержка, аналитик)")
    role_profile = profile.role(role)
    if role_profile is None:
        return Decision(False, f"роль {role.value} не описана в candidate.toml")
    if score < min_score:
        return Decision(False, f"оценка {score} ниже порога {min_score}")
    # Верх вилки (или «от», если верха нет) ниже минимума — вакансия точно не по деньгам.
    # Зарплата не указана — не отсеиваем
    top = salary_to or salary_from
    if role_profile.min_salary and top and currency in RUBLES and top < role_profile.min_salary:
        return Decision(False, f"зарплата до {top} ниже {role_profile.min_salary}")
    return Decision(True)
