import httpx
import pytest

from app.apply.vacancy_text import (
    MAX_DESCRIPTION_CHARS,
    PageTextSource,
    VacancyText,
    html_to_text,
    parse_habr_page,
    parse_hh_page,
    text_from_raw,
)
from app.sources.portals import Portal
from tests.conftest import FIXTURES

# Сокращённые настоящие страницы вакансий с полным описанием
HH_PAGE = (FIXTURES / "hh" / "vacancy_page_full.html").read_text(encoding="utf-8")
HABR_PAGE = (FIXTURES / "habr" / "vacancy_page_full.html").read_text(encoding="utf-8")
HH_PAGE_ID = "139080109"


def test_html_to_text_keeps_structure() -> None:
    html = (
        "<p><strong>Задачи:</strong></p>"
        "<ul><li>верстать&nbsp;экраны</li><li>писать   тесты</li></ul>"
        "<p>Строка<br>вторая</p>"
    )

    assert html_to_text(html) == "Задачи:\n- верстать экраны\n- писать тесты\nСтрока\nвторая"


def test_html_to_text_empty_and_truncated() -> None:
    assert html_to_text("   ") == ""
    assert len(html_to_text("<p>" + "а" * (MAX_DESCRIPTION_CHARS + 500) + "</p>")) == (
        MAX_DESCRIPTION_CHARS
    )


def test_parse_hh_page() -> None:
    text = parse_hh_page(HH_PAGE, HH_PAGE_ID)

    assert text.skills == ["TypeScript", "Linux"]
    assert text.letter_required is True
    assert text.has_test is True
    assert text.test_required is False
    assert text.external_url is None
    assert text.description.startswith("О нас\nМы разрабатываем собственные программные продукты")
    assert "<" not in text.description


def test_parse_hh_page_other_vacancy_id_has_no_response_block() -> None:
    text = parse_hh_page(HH_PAGE, "1")

    # Тест берётся только из блока отклика этой вакансии
    assert text.has_test is False
    assert text.description


def test_parse_hh_page_without_state() -> None:
    with pytest.raises(ValueError, match="HH-Lux-InitialState"):
        parse_hh_page("<html></html>", HH_PAGE_ID)


def test_parse_habr_page() -> None:
    text = parse_habr_page(HABR_PAGE)

    assert text.skills == ["CSS", "TypeScript", "JavaScript", "Git", "HTML", "React"]
    assert text.letter_required is False
    assert text.external_url is None
    assert text.description.startswith("Привет! Мы – PREAX")


def test_parse_habr_page_without_state() -> None:
    with pytest.raises(ValueError, match="data-ssr-state"):
        parse_habr_page("<html></html>")


def test_text_from_raw_superjob() -> None:
    raw = {
        "vacancyRichText": "<p>Ищем <b>frontend</b>-разработчика</p>",
        "candidat": "React, TypeScript",
        "work": None,
        "profession": "не попадает в описание",
    }

    assert text_from_raw(Portal.SUPERJOB, raw) == VacancyText(
        description="Ищем frontend-разработчика\nReact, TypeScript"
    )


def test_text_from_raw_nested_fields() -> None:
    raw = {"duty": "Поддержка пользователей", "requirement": {"qualification": "SQL", "x": 1}}

    assert text_from_raw(Portal.TRUDVSEM, raw).description == "Поддержка пользователей\nSQL"


def test_text_from_raw_unknown_fields() -> None:
    assert text_from_raw(Portal.GETMATCH, {}) == VacancyText()


async def test_page_source_reads_hh_page_and_raw_for_others() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if "hh.ru" in str(request.url):
            return httpx.Response(200, text=HH_PAGE)
        return httpx.Response(404)

    source = PageTextSource("test-agent", 0, transport=httpx.MockTransport(handler))
    try:
        hh = await source.get_text(HH_PAGE_ID, f"https://hh.ru/vacancy/{HH_PAGE_ID}", {})
        gone = await source.get_text("habr-1", "https://career.habr.com/vacancies/1", {})
        sj = await source.get_text("sj-5", "https://superjob.ru/5", {"candidat": "Опыт с React"})
    finally:
        await source.aclose()

    assert hh.letter_required is True
    # Страница удалена — описания нет, но и ошибки нет
    assert gone == VacancyText()
    assert sj.description == "Опыт с React"
    assert requested == [
        f"https://hh.ru/vacancy/{HH_PAGE_ID}",
        "https://career.habr.com/vacancies/1",
    ]
