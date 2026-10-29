"""getmatch.ru: вакансия из JSON-API сайта → общий формат (VacancyShort), id — «gm-».

Формат и место работы — список location_requirements: {"format": remote | hybrid |
office | relocation_company, "location_id": "kazan__tatarstan__russia", "ancestors": [...],
"city": "Казань", "exclude": false}.
"""

from datetime import UTC, datetime
from typing import Any

from app.sources.details import VacancyDetails, salary_dict
from app.sources.employers import employer_key
from app.sources.models import VacancyShort
from app.sources.portals import Portal, vacancy_id

BASE_URL = "https://getmatch.ru"
HIDDEN_COMPANY = "Компания скрыта"
# Форматы getmatch → форматы работы hh.ru
WORK_FORMATS = {
    "remote": ("REMOTE", "Удалённо"),
    "hybrid": ("HYBRID", "Гибрид"),
    "office": ("ON_SITE", "В офисе"),
}
CURRENCIES = {"RUB": "RUR"}


def requirements(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in raw.get("location_requirements") or [] if not r.get("exclude")]


def _published(value: str) -> datetime:
    # Время без часового пояса — считаем UTC. Если на деле это МСК, вакансия покажется
    # на 3 часа «свежее», но в окно поиска всё равно попадёт
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _salary(raw: dict[str, Any]) -> dict[str, Any] | None:
    if raw.get("salary_hidden"):
        return None
    currency = raw.get("salary_currency")
    return salary_dict(
        raw.get("salary_display_from"),
        raw.get("salary_display_to"),
        CURRENCIES.get(currency, currency) if currency else None,
        raw.get("salary_taxes") == "gross",
    )


def vacancy_from_api(raw: dict[str, Any]) -> VacancyShort:
    company = ((raw.get("company") or {}).get("name") or "").strip() or HIDDEN_COMPANY
    reqs = requirements(raw)
    cities = list(dict.fromkeys(r["city"] for r in reqs if r.get("city")))
    formats = list(dict.fromkeys(r["format"] for r in reqs if r.get("format") in WORK_FORMATS))
    return VacancyShort.model_validate(
        {
            "id": vacancy_id(Portal.GETMATCH, str(raw["id"])),
            "name": raw["position"].strip(),
            "area": {"id": "", "name": ", ".join(cities)},
            "salary": _salary(raw),
            # У скрытой компании id нет, иначе «Скрыть компанию» скрыла бы их все
            "employer": {
                "id": None if company == HIDDEN_COMPANY else employer_key(company),
                "name": company,
            },
            "published_at": _published(raw["published_at"]),
            "archived": not raw.get("is_active", True),
            "alternate_url": BASE_URL + raw["url"],
            "work_format": [
                {"id": WORK_FORMATS[f][0], "name": WORK_FORMATS[f][1]} for f in formats
            ],
        }
    )


def details_from_api(raw: dict[str, Any]) -> VacancyDetails:
    vacancy = vacancy_from_api(raw)
    salary = vacancy.salary
    return VacancyDetails(
        id=vacancy.id,
        name=vacancy.name,
        archived=vacancy.archived,
        employer_id=vacancy.employer.id,
        salary=salary_dict(salary.from_, salary.to, salary.currency, salary.gross)
        if salary
        else None,
        work_format=[f.id for f in vacancy.work_format],
        area=vacancy.area.name or None,
    )
