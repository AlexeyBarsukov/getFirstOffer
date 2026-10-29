"""Ответ API SuperJob → общий формат вакансии (VacancyShort), id — с префиксом «sj-»."""

import re
from datetime import UTC, datetime
from html import unescape
from typing import Any

from app.sources.details import VacancyDetails, salary_dict
from app.sources.employers import employer_key
from app.sources.models import VacancyShort
from app.sources.portals import Portal, vacancy_id

# place_of_work: 1 — у работодателя, 2 — на дому, 3 — разъездной, 0 — не имеет значения
PLACE_AT_HOME = 2
# experience SuperJob → опыт hh.ru (те же 4 ступени)
EXPERIENCE = {
    1: "noExperience",
    2: "between1And3",
    3: "between3And6",
    4: "moreThan6",
}
CURRENCIES = {"rub": "RUR", "usd": "USD", "eur": "EUR", "uah": "UAH", "uzs": "UZS"}
WORK_FORMAT_REMOTE = {"id": "REMOTE", "name": "Удалённо"}
_TEXT_FIELDS = ("profession", "candidat", "work", "compensation", "vacancyRichText")
# Именно про формат работы: «удалёнка», «удалённая работа», «работать удалённо», «из дома».
# Просто «удалённый» не подходит — в IT это ещё «удалённый доступ», «удалённая поддержка»
_REMOTE = re.compile(
    r"удал[её]нк\w*"
    r"|удал[её]нн\w*\s+(?:работ|формат|режим)\w*"
    r"|работ\w*\s+(?:полностью\s+)?удал[её]нно"
    r"|дистанционн\w*\s+(?:работ|формат)\w*"
    r"|из\s+дома|\bremote\b",
    re.I,
)
# «Никакой удалённой работы», «без удалёнки», «не удалённо»
_NEGATION = re.compile(r"(?:никак\w*|\bнет\b|\bбез\b|\bне\b)\W*\w*\W*$", re.I)


def is_remote(raw: dict[str, Any]) -> bool:
    """Удалёнку на SuperJob почти не отмечают полем, а пишут в тексте вакансии."""
    if (raw.get("place_of_work") or {}).get("id") == PLACE_AT_HOME:
        return True
    text = unescape(" ".join(str(raw.get(f) or "") for f in _TEXT_FIELDS))
    return any(
        not _NEGATION.search(text[max(0, m.start() - 25) : m.start()])
        for m in _REMOTE.finditer(text)
    )


def _salary(raw: dict[str, Any]) -> dict[str, Any] | None:
    # 0 — «не указана»
    currency = CURRENCIES.get(str(raw.get("currency") or "").lower())
    return salary_dict(
        raw.get("payment_from") or None, raw.get("payment_to") or None, currency, None
    )


def vacancy_from_api(raw: dict[str, Any]) -> VacancyShort:
    company = unescape(str(raw.get("firm_name") or "")).strip() or "Компания не указана"
    experience = raw.get("experience") or {}
    hh_experience = EXPERIENCE.get(int(experience.get("id") or 0))
    return VacancyShort.model_validate(
        {
            "id": vacancy_id(Portal.SUPERJOB, str(raw["id"])),
            "name": unescape(str(raw["profession"])).strip(),
            "area": {"id": "", "name": (raw.get("town") or {}).get("title") or ""},
            "salary": _salary(raw),
            # Анонимный работодатель — без id, иначе «Скрыть компанию» скрыла бы всех анонимов
            "employer": {
                "id": None if raw.get("anonymous") else employer_key(company),
                "name": company,
            },
            "published_at": datetime.fromtimestamp(int(raw["date_published"]), UTC),
            "archived": bool(raw.get("is_archive") or raw.get("is_closed")),
            "alternate_url": raw["link"],
            "experience": {"id": hh_experience, "name": experience.get("title") or ""}
            if hh_experience
            else None,
            "work_format": [WORK_FORMAT_REMOTE] if is_remote(raw) else [],
        }
    )


def details_from_api(raw: dict[str, Any]) -> VacancyDetails | None:
    """None — вакансию удалили (is_storage)."""
    if raw.get("is_storage"):
        return None
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
        experience=vacancy.experience.id if vacancy.experience else None,
        work_format=[f.id for f in vacancy.work_format],
        area=vacancy.area.name or None,
    )
