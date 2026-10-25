"""Поиск регионов hh.ru по названию (справочник GET /areas)."""

from collections.abc import Iterable

from app.services.presets import AREA_RUSSIA
from app.sources.hh_models import Area


def _normalize(name: str) -> str:
    return name.strip().lower().replace("ё", "е")


class AreaIndex:
    def __init__(self, countries: Iterable[Area]) -> None:
        self._names: dict[str, str] = {}
        # нормализованное название → [(id, в России ли)]
        self._by_name: dict[str, list[tuple[str, bool]]] = {}
        for country in countries:
            self._walk(country, in_russia=country.id == AREA_RUSSIA)

    def _walk(self, area: Area, *, in_russia: bool) -> None:
        self._names[area.id] = area.name
        self._by_name.setdefault(_normalize(area.name), []).append((area.id, in_russia))
        for child in area.areas:
            self._walk(child, in_russia=in_russia)

    @classmethod
    def fallback(cls) -> "AreaIndex":
        """Если справочник не загрузился — хотя бы «Россия»."""
        return cls([Area(id=AREA_RUSSIA, name="Россия")])

    def name(self, area_id: str) -> str:
        return self._names.get(area_id, f"регион {area_id}")

    def resolve(self, name: str) -> str | None:
        """id региона по названию. При совпадении названий предпочитаем регионы России."""
        candidates = self._by_name.get(_normalize(name), [])
        if not candidates:
            return None
        return sorted(candidates, key=lambda c: not c[1])[0][0]
