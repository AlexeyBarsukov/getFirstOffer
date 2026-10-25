from app.services.presets import AREA_RUSSIA, PRESETS
from tests.conftest import load_fixture


def it_role_ids() -> set[str]:
    categories = load_fixture("hh/professional_roles.json")["categories"]
    it = next(c for c in categories if c["id"] == "11")
    return {r["id"] for r in it["roles"]}


def test_presets_use_default_search_settings() -> None:
    assert set(PRESETS) == {"frontend", "backend", "support", "it"}
    for preset in PRESETS.values():
        assert preset.filters.areas == [AREA_RUSSIA]
        assert preset.filters.work_format == ["REMOTE"]
        assert preset.filters.experience == ["noExperience", "between1And3"]
        assert "senior" in preset.exclude_words


def test_preset_roles_exist_in_it_category() -> None:
    known = it_role_ids()
    for preset in PRESETS.values():
        assert preset.filters.professional_roles
        assert set(preset.filters.professional_roles) <= known, preset.key


def test_presets_do_not_share_exclude_words_list() -> None:
    PRESETS["frontend"].exclude_words.append("php")
    try:
        assert "php" not in PRESETS["backend"].exclude_words
    finally:
        PRESETS["frontend"].exclude_words.remove("php")
