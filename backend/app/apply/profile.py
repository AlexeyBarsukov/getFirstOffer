"""Профиль кандидата (candidate.toml в корне репозитория)."""

import enum
import tomllib
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field

DEFAULT_PROFILE_PATH = Path(__file__).resolve().parents[3] / "candidate.toml"


class Role(enum.StrEnum):
    FRONTEND = "frontend"
    QA = "qa"
    SUPPORT = "support"
    ANALYST = "analyst"
    OTHER = "other"


ROLE_TITLES = {
    Role.FRONTEND: "Frontend",
    Role.QA: "QA",
    Role.SUPPORT: "Техподдержка",
    Role.ANALYST: "Аналитик данных",
    Role.OTHER: "Другое",
}


class RoleProfile(BaseModel):
    title: str
    min_salary: int = 0
    # Под каким углом подавать опыт для этой роли
    angle: str = ""
    facts: list[str] = Field(default_factory=list)


class CandidateProfile(BaseModel):
    name: str
    telegram: str
    city: str
    experience_since: int
    # Технологии и инструменты, с которыми кандидат реально работал. Только их письмо может
    # называть опытом (letter_rules.unknown_tech)
    skills: list[str] = Field(default_factory=list)
    facts: list[str]
    do_not_claim: list[str] = Field(default_factory=list)
    roles: dict[Role, RoleProfile]

    def role(self, role: Role) -> RoleProfile | None:
        return self.roles.get(role)

    def experience_years(self, today: date | None = None) -> int:
        return (today or date.today()).year - self.experience_since

    def known_text(self) -> str:
        """Всё, что кандидат может заявить о себе: навыки и факты всех ролей."""
        parts = [*self.skills, *self.facts]
        for data in self.roles.values():
            parts += [data.angle, *data.facts]
        return "\n".join(parts)


def load_profile(path: Path = DEFAULT_PROFILE_PATH) -> CandidateProfile:
    with path.open("rb") as file:
        return CandidateProfile.model_validate(tomllib.load(file))
