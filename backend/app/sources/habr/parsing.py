"""Разбор ответов Хабр Карьеры: RSS-лента поиска, страница поиска и страница вакансии.

В страницах сайта есть встроенный JSON `<script type="application/json" data-ssr-state>`:
на странице поиска — `vacancies.list` и `vacancies.meta`, на странице вакансии — `vacancy`.
Вакансия приводится к общему формату (VacancyShort), id — с префиксом «habr-».
"""

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any

import structlog

from app.sources.details import VacancyDetails, salary_dict
from app.sources.employers import employer_key
from app.sources.models import VacancyShort
from app.sources.portals import Portal, vacancy_id
from app.sources.salary import parse_salary_text

logger = structlog.get_logger(__name__)

BASE_URL = "https://career.habr.com"
WORK_FORMAT_REMOTE = {"id": "REMOTE", "name": "Удалённо"}
QUALIFICATIONS = ("intern", "junior", "middle", "senior", "lead")

_SSR_STATE = re.compile(
    r'<script type="application/json" data-ssr-state="true">(.*?)</script>', re.S
)
# «Требуется «Frontend-разработчик (React)» (Москва, от 170 000 ₽)»
_RSS_TITLE = re.compile(r"^Требуется «(?P<name>.*)»(?:\s*\((?P<extra>[^()]*)\))?$", re.S)
_HASHTAG = re.compile(r"#(\w+)")


def _ssr_state(html: str) -> dict[str, Any]:
    match = _SSR_STATE.search(html)
    if not match:
        raise ValueError("на странице Хабра нет data-ssr-state")
    state: dict[str, Any] = json.loads(unescape(match.group(1)))
    return state


def _experience(qualification: str | None) -> dict[str, str] | None:
    """Квалификация Хабра (Middle) → поле experience: id в нижнем регистре, имя для показа."""
    if not qualification:
        return None
    return {"id": qualification.casefold(), "name": qualification}


def _salary(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    raw = raw or {}
    currency = raw.get("currency")
    return salary_dict(raw.get("from"), raw.get("to"), currency.upper() if currency else None, None)


def _cities(item: dict[str, Any]) -> str:
    return ", ".join(loc["title"] for loc in item.get("locations") or [] if loc.get("title"))


def vacancy_from_item(item: dict[str, Any]) -> VacancyShort:
    """Вакансия из JSON страницы поиска или страницы вакансии."""
    company = item.get("company") or {}
    company_name = company.get("title") or "Компания не указана"
    return VacancyShort.model_validate(
        {
            "id": vacancy_id(Portal.HABR, str(item["id"])),
            "name": item["title"],
            "area": {"id": "", "name": _cities(item)},
            "salary": _salary(item.get("salary")),
            # Ключ по названию, как у RSS, — чтобы «Скрыть компанию» работала для обоих
            "employer": {"id": employer_key(company_name), "name": company_name},
            "published_at": (item.get("publishedDate") or {})["date"],
            "archived": bool(item.get("archived")),
            "alternate_url": BASE_URL + item["href"],
            "experience": _experience(item.get("qualification")),
            "work_format": [WORK_FORMAT_REMOTE] if item.get("remoteWork") else [],
        }
    )


def parse_search_page(html: str) -> tuple[list[VacancyShort], int]:
    """Вакансии со страницы поиска и число страниц выдачи."""
    data = _ssr_state(html)["vacancies"]
    vacancies = []
    for item in data.get("list") or []:
        try:
            vacancies.append(vacancy_from_item(item))
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("habr_item_skipped", error=repr(exc))
    return vacancies, int((data.get("meta") or {}).get("totalPages") or 1)


def parse_vacancy_page(html: str) -> VacancyDetails:
    item = _ssr_state(html)["vacancy"]
    vacancy = vacancy_from_item(item)
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


def _parse_rss_item(item: ET.Element) -> VacancyShort:
    guid = (item.findtext("guid") or "").strip()
    title = unescape(item.findtext("title") or "").strip()
    match = _RSS_TITLE.match(title)
    if not guid.isdigit() or not match:
        raise ValueError(f"неожиданная запись RSS: {guid!r} {title!r}")
    # В скобках — города и зарплата через запятую: «Москва, от 170 000 ₽»
    parts = [p.strip() for p in (match.group("extra") or "").split(",") if p.strip()]
    salary = next((s for p in parts if (s := parse_salary_text(p))), None)
    cities = [p for p in parts if not re.search(r"\d", p)]
    description = unescape(item.findtext("description") or "")
    tags = {t.casefold() for t in _HASHTAG.findall(description)}
    qualification = next((q for q in QUALIFICATIONS if q in tags), None)
    company = unescape(item.findtext("author") or "").strip() or "Компания не указана"
    published = parsedate_to_datetime((item.findtext("pubDate") or "").strip())
    return VacancyShort.model_validate(
        {
            "id": vacancy_id(Portal.HABR, guid),
            "name": match.group("name").strip(),
            "area": {"id": "", "name": ", ".join(cities)},
            "salary": salary,
            "employer": {"id": employer_key(company), "name": company},
            "published_at": published,
            "alternate_url": (item.findtext("link") or "").strip(),
            "experience": _experience(qualification.capitalize() if qualification else None),
            "work_format": [WORK_FORMAT_REMOTE] if "Можно удалённо" in description else [],
        }
    )


def parse_feed(xml_text: str) -> list[VacancyShort]:
    root = ET.fromstring(xml_text)
    vacancies = []
    for item in root.iterfind("./channel/item"):
        try:
            vacancies.append(_parse_rss_item(item))
        except (ValueError, TypeError) as exc:
            # Одна странная запись не должна ломать всю ленту
            logger.warning("habr_rss_item_skipped", error=str(exc))
    return vacancies


def published_before(vacancies: list[VacancyShort], moment: datetime) -> bool:
    """Есть ли в выдаче (отсортированной по дате) вакансии старше moment."""
    return any(v.published_at < moment for v in vacancies)
