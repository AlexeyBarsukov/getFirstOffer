"""Площадки с вакансиями.

id вакансии hh.ru хранится как есть (так было до появления других площадок), у остальных
площадок — с префиксом: «habr-1000168870», «sj-51234567». Не двоеточие: оно разделяет поля
в callback_data кнопок Telegram, а id вакансии туда попадает.
"""

import enum


class Portal(enum.StrEnum):
    HH = "hh"
    HABR = "habr"
    SUPERJOB = "superjob"
    GETMATCH = "getmatch"
    TRUDVSEM = "trudvsem"
    RABOTA = "rabota"


PORTAL_TITLES = {
    Portal.HH: "hh.ru",
    Portal.HABR: "Хабр Карьера",
    Portal.SUPERJOB: "SuperJob",
    Portal.GETMATCH: "getmatch",
    Portal.TRUDVSEM: "Работа России",
    Portal.RABOTA: "Rabota.ru",
}

# «Открыть на …», «снята с публикации на …»
PORTAL_TITLES_PREPOSITIONAL = {
    Portal.HH: "hh.ru",
    Portal.HABR: "Хабр Карьере",
    Portal.SUPERJOB: "SuperJob",
    Portal.GETMATCH: "getmatch",
    Portal.TRUDVSEM: "«Работе России»",
    Portal.RABOTA: "Rabota.ru",
}

_ID_PREFIXES = {
    Portal.HABR: "habr",
    Portal.SUPERJOB: "sj",
    Portal.GETMATCH: "gm",
    Portal.TRUDVSEM: "td",
    Portal.RABOTA: "rb",
}
_PORTAL_BY_PREFIX = {prefix: portal for portal, prefix in _ID_PREFIXES.items()}

# Самый длинный id: «td-» + uuid без дефисов (32 символа)
VACANCY_ID_LENGTH = 48


def vacancy_id(portal: Portal, external_id: str) -> str:
    """id вакансии в нашей базе по id на площадке."""
    if portal == Portal.HH:
        return external_id
    return f"{_ID_PREFIXES[portal]}-{external_id}"


def split_vacancy_id(value: str) -> tuple[Portal, str]:
    """Площадка и id на площадке."""
    prefix, sep, external_id = value.partition("-")
    if not sep or prefix not in _PORTAL_BY_PREFIX:
        return Portal.HH, value
    return _PORTAL_BY_PREFIX[prefix], external_id


def portal_of(value: str) -> Portal:
    return split_vacancy_id(value)[0]
