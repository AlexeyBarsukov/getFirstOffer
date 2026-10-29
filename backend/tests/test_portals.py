import pytest

from app.bots.telegram.callbacks import CatalogCB
from app.bots.telegram.keyboards import catalog_card
from app.config import Settings
from app.db.models import EventType
from app.notifiers.messages import build_message
from app.sources.portals import VACANCY_ID_LENGTH, Portal, portal_of, split_vacancy_id, vacancy_id
from app.sources.registry import build_portals
from tests.test_messages import make_db_vacancy, make_event


@pytest.mark.parametrize(
    ("portal", "external_id", "expected"),
    [
        (Portal.HH, "110000001", "110000001"),
        (Portal.HABR, "1000168870", "habr-1000168870"),
        (Portal.SUPERJOB, "51234567", "sj-51234567"),
        (
            Portal.TRUDVSEM,
            "1a0e3e18aa9711f0b9afbdc82d0d5dcd",
            "td-1a0e3e18aa9711f0b9afbdc82d0d5dcd",
        ),
    ],
)
def test_vacancy_id_round_trip(portal: Portal, external_id: str, expected: str) -> None:
    value = vacancy_id(portal, external_id)

    assert value == expected
    assert len(value) <= VACANCY_ID_LENGTH
    assert split_vacancy_id(value) == (portal, external_id)


def test_longest_id_fits_catalog_button() -> None:
    longest = vacancy_id(Portal.TRUDVSEM, "f" * 32)

    packed = CatalogCB(action="skipped", vacancy_id=longest).pack()

    assert len(packed.encode()) <= 64
    assert CatalogCB.unpack(packed).vacancy_id == longest


def test_unknown_prefix_is_hh() -> None:
    assert portal_of("110000001") == Portal.HH
    assert portal_of("abc-1") == Portal.HH


def test_enabled_portals_from_env() -> None:
    settings = Settings(hh_user_agent="test", portals=" hh, habr ,")

    assert settings.enabled_portals == [Portal.HH, Portal.HABR]


def test_message_names_portal() -> None:
    vacancy = make_db_vacancy(
        id="habr-1000168870", url="https://career.habr.com/vacancies/1000168870"
    )

    message = build_message(make_event(EventType.NEW), vacancy, [])

    assert message.text.splitlines()[-1].endswith("· Хабр Карьера")
    assert message.buttons[0].text == "Открыть на Хабр Карьере"


def test_catalog_card_links_duplicates() -> None:
    hh = make_db_vacancy()
    habr = make_db_vacancy(id="habr-7", url="https://career.habr.com/vacancies/7")

    rows = catalog_card(hh, [habr]).inline_keyboard

    assert [(b.text, b.url) for b in rows[1]] == [
        ("↗️ Также на Хабр Карьере", "https://career.habr.com/vacancies/7")
    ]
    assert rows[-1][-1].text == "🏢 Скрыть компанию"


@pytest.mark.parametrize("key", [None, "", "  "])
def test_superjob_without_key_is_disabled(key: str | None) -> None:
    settings = Settings(hh_user_agent="test", portals="superjob", superjob_api_key=key)

    assert build_portals(settings) == {}
