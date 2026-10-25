from app.services.presets import AREA_KAZAN, AREA_RUSSIA, PRESETS
from tests.conftest import load_fixture


def it_role_ids() -> set[str]:
    categories = load_fixture("hh/professional_roles.json")["categories"]
    it = next(c for c in categories if c["id"] == "11")
    return {r["id"] for r in it["roles"]}


def test_notification_presets_use_default_search_settings() -> None:
    notify = {k: p for k, p in PRESETS.items() if not p.catalog}
    assert set(notify) == {"frontend", "backend", "support", "it"}
    for preset in notify.values():
        assert preset.filters.areas == [AREA_RUSSIA]
        assert preset.filters.work_format == ["REMOTE"]
        assert preset.filters.experience == ["noExperience", "between1And3"]
        assert "senior" in preset.exclude_words


def test_catalog_presets_cover_remote_and_kazan() -> None:
    remote, kazan = PRESETS["frontend_all"], PRESETS["frontend_kazan"]

    assert remote.catalog and kazan.catalog
    assert (remote.filters.areas, remote.filters.work_format) == ([AREA_RUSSIA], ["REMOTE"])
    assert (kazan.filters.areas, kazan.filters.work_format) == ([AREA_KAZAN], ["ON_SITE", "HYBRID"])
    for preset in (remote, kazan):
        # Любой опыт, без ограничения по ролям — только по названию вакансии
        assert preset.filters.experience == []
        assert preset.filters.professional_roles == []
        assert preset.filters.text and "react" in preset.filters.text
        assert "senior" not in preset.exclude_words
        assert "lead" in preset.exclude_words


def test_preset_roles_exist_in_it_category() -> None:
    known = it_role_ids()
    for preset in PRESETS.values():
        assert set(preset.filters.professional_roles) <= known, preset.key


def test_presets_do_not_share_exclude_words_list() -> None:
    PRESETS["frontend"].exclude_words.append("php")
    try:
        assert "php" not in PRESETS["backend"].exclude_words
    finally:
        PRESETS["frontend"].exclude_words.remove("php")
