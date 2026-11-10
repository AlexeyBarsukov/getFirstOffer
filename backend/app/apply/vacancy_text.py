"""Полный текст вакансии для анализа и письма.

hh.ru и Хабр: страница вакансии (встроенный JSON). Остальные площадки — то, что уже лежит
в vacancies.raw (в ответах их API есть описание).
"""

import json
import re
from html import unescape
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from app.sources.hh.details import (
    PAGE_REQUEST_INTERVAL,
    VacancyHiddenError,
    page_state,
    page_state_from_error,
)
from app.sources.http import HHAPIError, RetryingClient
from app.sources.portals import Portal, split_vacancy_id

_HABR_STATE = re.compile(r"<script[^>]*data-ssr-state[^>]*>(.*?)</script>", re.S)
# Описания бывают огромными; модели на процессоре длинный промпт обходится дорого
MAX_DESCRIPTION_CHARS = 6000


class VacancyText(BaseModel):
    description: str = ""
    skills: list[str] = Field(default_factory=list)
    # Работодатель требует сопроводительное письмо
    letter_required: bool = False
    # У вакансии есть тест (на hh — «пройдите тест при отклике»)
    has_test: bool = False
    test_required: bool = False
    # Отклик на сайте компании, а не на площадке
    external_url: str | None = None


def html_to_text(html: str) -> str:
    """HTML описания → текст с переносами строк и «- » у пунктов списков."""
    if not html.strip():
        return ""
    tree = HTMLParser(f"<div>{html}</div>")
    for li in tree.css("li"):
        li.insert_before("\n- ")
    for tag in tree.css("br"):
        tag.insert_before("\n")
    for block in tree.css("p, div, h1, h2, h3, h4, ul, ol"):
        block.insert_after("\n")
    root = tree.body or tree.root
    text = root.text(separator="", strip=False) if root else ""
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in unescape(text).splitlines()]
    result = "\n".join(line for line in lines if line)
    return result[:MAX_DESCRIPTION_CHARS]


def parse_hh_page(html: str, vacancy_id: str) -> VacancyText:
    """Заглушка антибота → SourceThrottledError, скрытая вакансия → VacancyHiddenError."""
    state = page_state(html)
    vacancy = state["vacancyView"]["vacancyFull"]["vacancy"]
    # Блок про отклик: тест и обязательность письма (в самой vacancy их нет)
    response = (state.get("applicantVacancyResponseStatuses") or {}).get(str(vacancy_id)) or {}
    test = response.get("test") or {}
    short = response.get("shortVacancy") or {}
    return VacancyText(
        description=html_to_text(vacancy.get("description") or ""),
        skills=[str(s) for s in vacancy.get("keySkills") or []],
        letter_required=bool(
            short.get("responseLetterRequired") or vacancy.get("responseLetterRequired")
        ),
        has_test=bool(test.get("hasTests")),
        test_required=bool(test.get("required")),
        external_url=vacancy.get("responseUrl") or None,
    )


def parse_habr_page(html: str) -> VacancyText:
    match = _HABR_STATE.search(html)
    if not match:
        raise ValueError("на странице Хабра нет data-ssr-state")
    vacancy = json.loads(unescape(match.group(1)))["vacancy"]
    skills = [
        str(s.get("title") if isinstance(s, dict) else s) for s in vacancy.get("skills") or []
    ]
    return VacancyText(
        description=html_to_text(vacancy.get("description") or ""),
        skills=[s for s in skills if s and s != "None"],
        external_url=vacancy.get("externalLink") or None,
    )


# Поля с описанием в ответах API (лежат в vacancies.raw)
_RAW_TEXT_FIELDS: dict[Portal, tuple[str, ...]] = {
    Portal.SUPERJOB: ("vacancyRichText", "candidat", "work", "compensation"),
    Portal.GETMATCH: ("offer_description", "description", "requirements"),
    Portal.TRUDVSEM: ("duty", "requirement", "addition"),
}


def _collect_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _collect_text(v)]
    if isinstance(value, list):
        return [t for v in value for t in _collect_text(v)]
    return []


def text_from_raw(portal: Portal, raw: dict[str, Any]) -> VacancyText:
    parts: list[str] = []
    for key in _RAW_TEXT_FIELDS.get(portal, ()):
        parts.extend(_collect_text(raw.get(key)))
    return VacancyText(description=html_to_text("<br>".join(p for p in parts if p.strip())))


class VacancyTextSource(Protocol):
    async def aclose(self) -> None: ...

    async def get_text(self, vacancy_id: str, url: str, raw: dict[str, Any]) -> VacancyText: ...


class PageTextSource:
    """Страницы hh.ru и Хабра; для остальных площадок — vacancies.raw."""

    def __init__(
        self,
        user_agent: str,
        min_request_interval: float = PAGE_REQUEST_INTERVAL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = RetryingClient(
            "", {"User-Agent": user_agent}, min_request_interval, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_text(self, vacancy_id: str, url: str, raw: dict[str, Any]) -> VacancyText:
        portal, external_id = split_vacancy_id(vacancy_id)
        if portal not in (Portal.HH, Portal.HABR):
            return text_from_raw(portal, raw)
        try:
            try:
                response = await self._http.get(url)
            except HHAPIError as exc:
                if exc.status_code == 404:
                    return VacancyText()
                if portal == Portal.HH:
                    page_state_from_error(exc)
                raise
            if portal == Portal.HH:
                return parse_hh_page(response.text, external_id)
            return parse_habr_page(response.text)
        except VacancyHiddenError:
            # Работодатель скрыл вакансию: описания нет, письмо по названию
            return VacancyText()
