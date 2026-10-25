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
AREA_KAZAN = "88"
WORK_FORMAT_REMOTE = "REMOTE"
EXPERIENCE_NONE = "noExperience"
EXPERIENCE_1_3 = "between1And3"

# Варианты для переключателей в боте и на сайте (порядок = порядок кнопок)
IT_ROLE_NAMES = {
    ROLE_DEVELOPER: "Разработчик",
    ROLE_SUPPORT: "Техподдержка",
    ROLE_TESTER: "Тестировщик",
    ROLE_DEVOPS: "DevOps",
    ROLE_SYSADMIN: "Сисадмин",
    ROLE_SYSTEM_ENGINEER: "Системный инженер",
    ROLE_NETWORK_ENGINEER: "Сетевой инженер",
    ROLE_SECURITY: "Инфобез",
    ROLE_ANALYST: "Аналитик",
    ROLE_SYSTEM_ANALYST: "Системный аналитик",
    ROLE_DATA_ANALYST: "Аналитик данных",
    ROLE_DATA_SCIENTIST: "Data Scientist",
    ROLE_TECH_WRITER: "Техписатель",
}
EXPERIENCE_NAMES = {
    EXPERIENCE_NONE: "Без опыта",
    EXPERIENCE_1_3: "1–3 года",
    "between3And6": "3–6 лет",
    "moreThan6": "Больше 6 лет",
}
WORK_FORMAT_NAMES = {
    WORK_FORMAT_REMOTE: "Удалённо",
    "HYBRID": "Гибрид",
    "ON_SITE": "В офисе",
    "FIELD_WORK": "Разъездной",
}

DEFAULT_EXCLUDE_WORDS = ["senior", "lead", "ведущий", "руководитель", "1С", "1C"]
# Для полной подборки отсекаем только руководящие роли — откликаемся почти на всё
CATALOG_EXCLUDE_WORDS = ["lead", "тимлид", "руководитель", "head", "1С", "1C"]
FRONTEND_TITLE_QUERY = (
    'NAME:(frontend OR "front-end" OR фронтенд OR react OR reactjs OR next.js OR vue OR angular)'
)


class Preset(BaseModel):
    key: str
    title: str
    filters: SearchFilters
    exclude_words: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDE_WORDS))
    # Полная подборка (команда /catalog), а не только уведомления о новых
    catalog: bool = False


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
            key="frontend_all",
            title="Frontend: вся удалёнка",
            filters=SearchFilters(
                text=FRONTEND_TITLE_QUERY, areas=[AREA_RUSSIA], work_format=[WORK_FORMAT_REMOTE]
            ),
            exclude_words=list(CATALOG_EXCLUDE_WORDS),
            catalog=True,
        ),
        Preset(
            key="frontend_kazan",
            title="Frontend: Казань (офис/гибрид)",
            filters=SearchFilters(
                text=FRONTEND_TITLE_QUERY, areas=[AREA_KAZAN], work_format=["ON_SITE", "HYBRID"]
            ),
            exclude_words=list(CATALOG_EXCLUDE_WORDS),
            catalog=True,
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
