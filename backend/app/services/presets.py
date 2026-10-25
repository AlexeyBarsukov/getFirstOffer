"""Готовые подписки. После создания подписки все поля можно менять."""

from pydantic import BaseModel, Field

from app.sources.base import SearchFilters

# id из GET /professional_roles, категория 11 «Информационные технологии»
ROLE_DEVELOPER = "96"  # Программист, разработчик
ROLE_SUPPORT = "121"  # Специалист технической поддержки
ROLE_TESTER = "124"  # Тестировщик
ROLE_DEVOPS = "160"  # DevOps-инженер
ROLE_SYSADMIN = "113"  # Системный администратор
ROLE_SYSTEM_ENGINEER = "114"  # Системный инженер
ROLE_NETWORK_ENGINEER = "112"  # Сетевой инженер
ROLE_SECURITY = "116"  # Специалист по информационной безопасности
ROLE_ANALYST = "10"  # Аналитик
ROLE_SYSTEM_ANALYST = "148"  # Системный аналитик
ROLE_DATA_ANALYST = "156"  # BI-аналитик, аналитик данных
ROLE_DATA_SCIENTIST = "165"  # Дата-сайентист
ROLE_TECH_WRITER = "126"  # Технический писатель

AREA_RUSSIA = "113"
WORK_FORMAT_REMOTE = "REMOTE"
EXPERIENCE_NONE = "noExperience"
EXPERIENCE_1_3 = "between1And3"

DEFAULT_EXCLUDE_WORDS = ["senior", "lead", "ведущий", "руководитель", "1С", "1C"]


class Preset(BaseModel):
    key: str
    title: str
    filters: SearchFilters
    exclude_words: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDE_WORDS))


def _filters(roles: list[str], text: str | None = None) -> SearchFilters:
    return SearchFilters(
        text=text,
        professional_roles=roles,
        areas=[AREA_RUSSIA],
        work_format=[WORK_FORMAT_REMOTE],
        experience=[EXPERIENCE_NONE, EXPERIENCE_1_3],
    )


PRESETS: dict[str, Preset] = {
    p.key: p
    for p in [
        Preset(
            key="frontend",
            title="Frontend",
            filters=_filters(
                [ROLE_DEVELOPER],
                "NAME:(frontend OR фронтенд OR react OR vue OR angular"
                " OR javascript OR typescript)",
            ),
        ),
        Preset(
            key="backend",
            title="Backend",
            filters=_filters(
                [ROLE_DEVELOPER],
                "NAME:(backend OR бэкенд OR python OR java OR golang OR node OR php)",
            ),
        ),
        Preset(
            key="support",
            title="Техподдержка",
            filters=_filters([ROLE_SUPPORT]),
        ),
        Preset(
            key="it",
            title="IT в целом",
            filters=_filters(
                [
                    ROLE_DEVELOPER,
                    ROLE_SUPPORT,
                    ROLE_TESTER,
                    ROLE_DEVOPS,
                    ROLE_SYSADMIN,
                    ROLE_SYSTEM_ENGINEER,
                    ROLE_NETWORK_ENGINEER,
                    ROLE_SECURITY,
                    ROLE_ANALYST,
                    ROLE_SYSTEM_ANALYST,
                    ROLE_DATA_ANALYST,
                    ROLE_DATA_SCIENTIST,
                    ROLE_TECH_WRITER,
                ]
            ),
        ),
    ]
}
