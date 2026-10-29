import pytest

from app.services.presets import FRONTEND_TITLE_QUERY
from app.sources.keywords import Keywords, matches_title, parse_query


def test_catalog_preset_query() -> None:
    keywords = parse_query(FRONTEND_TITLE_QUERY)

    # reactjs найдётся по react — отдельный запрос не нужен
    assert keywords == Keywords(
        [
            "frontend",
            "front-end",
            "фронтенд",
            "react",
            "next.js",
            "vue",
            "angular",
            "fullstack",
            "full-stack",
            "full stack",
            "фулстек",
            "фуллстек",
        ],
        title_only=True,
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, None),
        ("  ", None),
        ("frontend", Keywords(["frontend"], title_only=False)),
        ("react OR vue", Keywords(["react", "vue"], title_only=False)),
        ("NAME:react", Keywords(["react"], title_only=True)),
        ('NAME:("react native" OR flutter)', Keywords(["react native", "flutter"], True)),
        ("react AND NOT angular", Keywords(["react angular"], title_only=False)),
        ("React OR react", Keywords(["React"], title_only=False)),
    ],
)
def test_parse_query(text: str | None, expected: Keywords | None) -> None:
    assert parse_query(text) == expected


def test_matches_title_ignores_case() -> None:
    keywords = Keywords(["next.js", "фронтенд"], title_only=True)

    assert matches_title("Senior Next.js developer", keywords)
    assert matches_title("Фронтенд-разработчик", keywords)
    assert not matches_title("QA-инженер (React-проекты)", keywords)
