from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.services.presets import PRESETS
from app.sources.employers import employer_key
from app.sources.hh_rss import (
    RSS_PATH,
    HHRssSource,
    build_rss_params,
    parse_feed,
    parse_salary_text,
)
from app.sources.http import HHAPIError
from tests.conftest import FIXTURES, FakeSleep

# Настоящий ответ https://hh.ru/search/vacancy/rss (25.10.2026)
RECORDED_FEED = (FIXTURES / "hh" / "rss_search.xml").read_text(encoding="utf-8")
MSK = timezone(timedelta(hours=3))


def make_feed(*items: tuple[str, str, str]) -> str:
    """items: (id, title, pubDate)."""
    body = "".join(
        f"<item><pubDate>{pub}</pubDate><title>{title}</title>"
        f"<link>https://hh.ru/vacancy/{vid}</link>"
        "<description><![CDATA[<p>Вакансия компании: ООО &quot;Ромашка&quot;</p> "
        "<p>Регион: Москва</p> "
        "<p>Предполагаемый уровень месячного дохода: от 100 000 ₽</p>]]>"
        "</description></item>"
        for vid, title, pub in items
    )
    return (
        f"<?xml version='1.0' encoding='utf-8'?><rss version='2.0'><channel>{body}</channel></rss>"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("от 100 000 ₽", (100000, None, "RUR")),
        ("до 450 000 ₽", (None, 450000, "RUR")),
        ("от 250 000 до 300 000 ₽", (250000, 300000, "RUR")),
        ("от 3 000 до 4 500 $", (3000, 4500, "USD")),
        ("от 1 000 €", (1000, None, "EUR")),
    ],
)
def test_parse_salary_text(text: str, expected: tuple[int | None, int | None, str]) -> None:
    salary = parse_salary_text(text)
    assert salary is not None
    assert (salary["from"], salary["to"], salary["currency"]) == expected
    assert salary["gross"] is None


def test_parse_salary_text_not_specified() -> None:
    assert parse_salary_text("не указан") is None
    assert parse_salary_text("") is None


def test_parse_recorded_feed() -> None:
    vacancies = parse_feed(RECORDED_FEED)

    assert len(vacancies) == 20
    first = vacancies[0]
    assert first.id == "133629890"
    assert first.name == "Database Developer (MongoDB)"
    assert first.employer.name == "Altenar"
    assert first.employer.id == employer_key("Altenar")
    assert first.area.name == "Владимир"
    assert first.salary is None
    assert first.alternate_url == "https://hh.ru/vacancy/133629890"
    assert first.published_at.tzinfo is not None
    # Лента отсортирована от новых к старым
    dates = [v.published_at for v in vacancies]
    assert dates == sorted(dates, reverse=True)
    assert any(v.salary is not None and v.salary.currency == "RUR" for v in vacancies)


def test_parse_feed_unescapes_and_skips_broken_items() -> None:
    feed = make_feed(("1", "Frontend &amp; React", "2026-10-25T20:00:00+03:00"))
    feed = feed.replace("</channel>", "<item><title>без ссылки</title></item></channel>")

    [vacancy] = parse_feed(feed)

    assert vacancy.name == "Frontend & React"
    assert vacancy.employer.name == 'ООО "Ромашка"'
    assert vacancy.salary is not None
    assert vacancy.salary.from_ == 100000


def test_employer_key_is_stable_and_short() -> None:
    assert employer_key("Altenar") == employer_key("  altenar ")
    assert employer_key("Altenar") != employer_key("Яндекс")
    # Влезает в callback_data «hide:<key>:0» (лимит Telegram — 64 байта)
    assert len(f"hide:{employer_key('Очень длинное название компании ООО')}:0".encode()) < 64


def test_build_rss_params_uses_search_filters_without_paging() -> None:
    params = build_rss_params(PRESETS["frontend"].filters)

    keys = [k for k, _ in params]
    assert "page" not in keys and "per_page" not in keys and "date_from" not in keys
    assert ("work_format", "REMOTE") in params
    assert ("experience", "noExperience") in params
    assert ("order_by", "publication_time") in params


async def test_search_new_filters_by_date(fake_sleep: FakeSleep) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        feed = make_feed(
            ("2", "Новая", "2026-10-25T20:05:00+03:00"),
            ("1", "Старая", "2026-10-25T19:00:00+03:00"),
        )
        return httpx.Response(200, text=feed)

    source = HHRssSource("ua (a@b.c)", transport=httpx.MockTransport(handler), sleep=fake_sleep)
    async with source:
        found = await source.search_new(
            PRESETS["frontend"].filters, datetime(2026, 10, 25, 20, 0, tzinfo=MSK)
        )

    assert [v.id for v in found] == ["2"]
    assert requests[0].url.path == RSS_PATH
    assert requests[0].headers["User-Agent"] == "ua (a@b.c)"
    assert requests[0].url.params.get_list("experience") == ["noExperience", "between1And3"]


async def test_search_new_retries_server_errors(fake_sleep: FakeSleep) -> None:
    responses = iter([httpx.Response(503), httpx.Response(200, text=RECORDED_FEED)])
    source = HHRssSource(
        "ua",
        transport=httpx.MockTransport(lambda _: next(responses)),
        sleep=fake_sleep,
        min_request_interval=0,
    )

    async with source:
        found = await source.search_new(
            PRESETS["frontend"].filters, datetime(2000, 1, 1, tzinfo=MSK)
        )

    assert len(found) == 20
    assert fake_sleep.calls == [1.0]


async def test_search_new_raises_on_forbidden(fake_sleep: FakeSleep) -> None:
    source = HHRssSource(
        "ua", transport=httpx.MockTransport(lambda _: httpx.Response(403)), sleep=fake_sleep
    )

    async with source:
        with pytest.raises(HHAPIError):
            await source.search_new(PRESETS["frontend"].filters, datetime(2000, 1, 1, tzinfo=MSK))
