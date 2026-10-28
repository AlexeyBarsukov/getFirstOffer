import pytest

from app.services.areas import AreaIndex
from app.services.subscriptions import (
    InvalidInputError,
    parse_keywords,
    parse_list,
    parse_salary,
    toggle,
    validate_filters,
)
from app.sources.base import SearchFilters
from app.sources.hh.models import Area


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("react, vue, typescript", "NAME:(react OR vue OR typescript)"),
        ("react\nnext.js", "NAME:(react OR next.js)"),
        ("frontend developer, react", 'NAME:("frontend developer" OR react)'),
        ("NAME:(react OR vue) NOT angular", "NAME:(react OR vue) NOT angular"),
        ("python AND django", "python AND django"),
        ("-", None),
        ("   ", None),
    ],
)
def test_parse_keywords(text: str, expected: str | None) -> None:
    assert parse_keywords(text) == expected


def test_parse_list_dedupes_and_clears() -> None:
    assert parse_list("senior, lead,  senior ,\n1С") == ["senior", "lead", "1С"]
    assert parse_list("-") == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [("80000", 80000), ("100 000", 100000), ("150000₽", 150000), ("0", None), ("-", None)],
)
def test_parse_salary(text: str, expected: int | None) -> None:
    assert parse_salary(text) == expected


def test_parse_salary_rejects_garbage() -> None:
    with pytest.raises(InvalidInputError):
        parse_salary("много")


def test_validate_filters_requires_keywords_or_roles() -> None:
    with pytest.raises(InvalidInputError, match="ключевые слова или хотя бы одна роль"):
        validate_filters(SearchFilters(areas=["113"]))
    validate_filters(SearchFilters(areas=["113"], professional_roles=["96"]))
    validate_filters(SearchFilters(areas=["113"], text="react"))


def test_validate_filters_requires_area() -> None:
    with pytest.raises(InvalidInputError, match="регион"):
        validate_filters(SearchFilters(text="react"))


def test_toggle() -> None:
    assert toggle(["a", "b"], "a") == ["b"]
    assert toggle(["b"], "a") == ["b", "a"]


def make_areas() -> AreaIndex:
    russia = Area(
        id="113",
        name="Россия",
        areas=[
            Area(id="1", name="Москва", parent_id="113"),
            Area(
                id="1620",
                name="Республика Марий Эл",
                parent_id="113",
                areas=[Area(id="1621", name="Йошкар-Ола")],
            ),
            Area(id="2019", name="Королёв", parent_id="113"),
        ],
    )
    other = Area(id="5", name="Украина", areas=[Area(id="999", name="Москва")])  # тёзка
    return AreaIndex([other, russia])


def test_area_index_resolves_names() -> None:
    areas = make_areas()

    assert areas.resolve("россия") == "113"
    assert areas.resolve(" Йошкар-Ола ") == "1621"
    assert areas.resolve("Королев") == "2019"  # ё/е
    assert areas.resolve("Москва") == "1"  # регион России важнее тёзки
    assert areas.resolve("Атлантида") is None
    assert areas.name("1621") == "Йошкар-Ола"
    assert areas.name("777") == "регион 777"


def test_area_index_fallback() -> None:
    assert AreaIndex.fallback().resolve("Россия") == "113"
