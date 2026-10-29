"""«Работа России» (trudvsem.ru): вакансия из открытого API → общий формат, id — «td-».

https://opendata.trudvsem.ru/api/v1/vacancies — ответ:
{"meta": {"total": N}, "results": {"vacancies": [{"vacancy": {...}}, ...]}}.
"""

import re
from datetime import UTC, date, datetime
from typing import Any

from app.sources.details import VacancyDetails, salary_dict
from app.sources.employers import employer_key
from app.sources.models import VacancyShort
from app.sources.portals import Portal, vacancy_id

REMOTE_EMPLOYMENT = "Дистанционная (удаленная) работа"
WORK_FORMAT_REMOTE = {"id": "REMOTE", "name": "Удалённо"}
# https://trudvsem.ru/vacancy/card/{ОГРН компании}/{uuid вакансии}
_CARD_URL = re.compile(r"/vacancy/card/(?P<company>[^/]+)/(?P<id>[0-9a-f-]{36})")


def vacancies(data: dict[str, Any]) -> list[dict[str, Any]]:
    items = (data.get("results") or {}).get("vacancies") or []
    return [item["vacancy"] for item in items if item.get("vacancy")]


def experience_code(years: int | None) -> str | None:
    """Требуемый опыт в годах → ступень опыта hh.ru."""
    if years is None:
        return None
    if years <= 0:
        return "noExperience"
    if years < 3:
        return "between1And3"
    if years < 6:
        return "between3And6"
    return "moreThan6"


def is_remote(raw: dict[str, Any]) -> bool:
    return raw.get("employment") == REMOTE_EMPLOYMENT


def is_quota_only(raw: dict[str, Any]) -> bool:
    """Место только по квоте (для людей с инвалидностью, участников СВО и т.п.)."""
    place = raw.get("workPlaceType") or {}
    return bool(place.get("workPlaceQuota")) and not place.get("workPlaceOrdinary")


def city_text(raw: dict[str, Any]) -> str:
    """Адреса вакансии одной строкой — для проверки города."""
    addresses = (raw.get("addresses") or {}).get("address") or []
    return " ".join(str(a.get("location") or "") for a in addresses)


def _published(raw: dict[str, Any]) -> datetime:
    # date_modify — последнее обновление: вакансии висят годами и продлеваются,
    # creation-date бывает и 2022 года
    if modified := raw.get("date_modify"):
        return datetime.fromisoformat(modified)
    created = date.fromisoformat(raw["creation-date"])
    return datetime(created.year, created.month, created.day, tzinfo=UTC)


def vacancy_from_api(raw: dict[str, Any]) -> VacancyShort:
    company = ((raw.get("company") or {}).get("name") or "").strip() or "Компания не указана"
    years = (raw.get("requirement") or {}).get("experience")
    code = experience_code(int(years) if years is not None else None)
    return VacancyShort.model_validate(
        {
            "id": vacancy_id(Portal.TRUDVSEM, str(raw["id"]).replace("-", "")),
            "name": str(raw["job-name"]).strip(),
            "area": {"id": "", "name": (raw.get("region") or {}).get("name") or ""},
            # 0 — «не указана»; валюта всегда рубли (««руб.»»)
            "salary": salary_dict(
                raw.get("salary_min") or None, raw.get("salary_max") or None, "RUR", None
            ),
            "employer": {"id": employer_key(company), "name": company},
            "published_at": _published(raw),
            "alternate_url": raw["vac_url"],
            "experience": {"id": code, "name": f"Опыт от {years} г."} if code else None,
            "work_format": [WORK_FORMAT_REMOTE] if is_remote(raw) else [],
        }
    )


def card_ids(url: str) -> tuple[str, str] | None:
    """ОГРН компании и uuid вакансии из ссылки на карточку."""
    match = _CARD_URL.search(url)
    return (match.group("company"), match.group("id")) if match else None


def details_from_api(raw: dict[str, Any]) -> VacancyDetails:
    vacancy = vacancy_from_api(raw)
    salary = vacancy.salary
    return VacancyDetails(
        id=vacancy.id,
        name=vacancy.name,
        archived=False,
        employer_id=vacancy.employer.id,
        salary=salary_dict(salary.from_, salary.to, salary.currency, salary.gross)
        if salary
        else None,
        experience=vacancy.experience.id if vacancy.experience else None,
        work_format=[f.id for f in vacancy.work_format],
        area=vacancy.area.name or None,
    )
