"""Бизнес-логика подписок — общая для Telegram-бота и (позже) веб-интерфейса.

Изменения не коммитятся здесь: транзакцией управляет вызывающий код.
После изменения фильтров стоит поставить внеочередную проверку подписки.
"""

import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Subscription
from app.db.repositories import SubscriptionRepository
from app.services.areas import AreaIndex
from app.services.presets import (
    EXPERIENCE_NAMES,
    IT_ROLE_NAMES,
    PRESETS,
    WORK_FORMAT_NAMES,
)
from app.sources.base import SearchFilters

CLEAR_VALUES = {"-", "—", "–"}
MAX_NAME_LENGTH = 100

# Поле SearchFilters → допустимые значения переключателя
TOGGLE_FIELDS: dict[str, dict[str, str]] = {
    "professional_roles": IT_ROLE_NAMES,
    "experience": EXPERIENCE_NAMES,
    "work_format": WORK_FORMAT_NAMES,
}


class InvalidInputError(ValueError):
    """Ошибка ввода пользователя. Текст сообщения показывается пользователю как есть."""


def parse_keywords(text: str) -> str | None:
    """«react, vue, typescript» → NAME:(react OR vue OR typescript).

    Запрос на языке hh.ru (с NAME:, AND, OR, NOT) сохраняется как есть. «-» — без ключевых слов.
    """
    text = text.strip()
    if not text or text in CLEAR_VALUES:
        return None
    if re.search(r"\b(AND|OR|NOT)\b|:", text):
        return text
    words = [w.strip() for w in re.split(r"[,;\n]", text) if w.strip()]
    if not words:
        return None
    quoted = [f'"{w}"' if " " in w else w for w in words]
    return f"NAME:({' OR '.join(quoted)})"


def parse_list(text: str) -> list[str]:
    """Список через запятую или с новой строки. «-» — пустой список."""
    text = text.strip()
    if text in CLEAR_VALUES:
        return []
    seen: dict[str, None] = {}
    for item in re.split(r"[,;\n]", text):
        if item := item.strip():
            seen.setdefault(item, None)
    return list(seen)


def parse_salary(text: str) -> int | None:
    text = text.strip()
    if text in CLEAR_VALUES:
        return None
    digits = re.sub(r"[\s _]", "", text).removesuffix("₽").removesuffix("р")
    if not digits.isdigit():
        raise InvalidInputError("Нужно число, например 80000. «0» или «-» — зарплата не важна.")
    value = int(digits)
    return value or None


def validate_filters(filters: SearchFilters) -> None:
    if not filters.text and not filters.professional_roles:
        raise InvalidInputError(
            "Нужны ключевые слова или хотя бы одна роль — иначе придут все вакансии подряд."
        )
    if not filters.areas:
        raise InvalidInputError("Нужен хотя бы один регион.")


def toggle(values: list[str], value: str) -> list[str]:
    return [v for v in values if v != value] if value in values else [*values, value]


class SubscriptionService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = SubscriptionRepository(session)

    async def get_owned(self, user_id: int, subscription_id: int) -> Subscription | None:
        subscription = await self._repo.get(subscription_id)
        if subscription is None or subscription.user_id != user_id:
            return None
        return subscription

    async def list_for_user(self, user_id: int) -> list[Subscription]:
        return await self._repo.list_for_user(user_id)

    async def create_from_preset(self, user_id: int, preset_key: str) -> Subscription:
        preset = PRESETS[preset_key]
        return await self._repo.create(
            user_id,
            preset.title,
            preset.filters,
            list(preset.exclude_words),
            catalog=preset.catalog,
        )

    async def _save_filters(self, subscription: Subscription, filters: SearchFilters) -> None:
        validate_filters(filters)
        await self._repo.update_filters(subscription.id, filters=filters)

    async def set_keywords(self, subscription: Subscription, text: str) -> None:
        filters = subscription.search_filters.model_copy(update={"text": parse_keywords(text)})
        await self._save_filters(subscription, filters)

    async def set_salary(self, subscription: Subscription, text: str) -> None:
        salary = parse_salary(text)
        filters = subscription.search_filters.model_copy(update={"salary": salary})
        await self._save_filters(subscription, filters)

    async def set_areas(self, subscription: Subscription, text: str, areas: AreaIndex) -> None:
        names = parse_list(text)
        if not names:
            raise InvalidInputError("Нужен хотя бы один регион, например: Россия")
        resolved = {name: areas.resolve(name) for name in names}
        unknown = [name for name, area_id in resolved.items() if area_id is None]
        if unknown:
            raise InvalidInputError(
                f"Не нашёл регион: {', '.join(unknown)}. Напиши как на hh.ru: Россия, Москва, "
                "Санкт-Петербург, Казахстан…"
            )
        area_ids = list(dict.fromkeys(a for a in resolved.values() if a))
        filters = subscription.search_filters.model_copy(update={"areas": area_ids})
        await self._save_filters(subscription, filters)

    async def toggle_value(self, subscription: Subscription, field: str, value: str) -> None:
        if value not in TOGGLE_FIELDS.get(field, {}):
            raise InvalidInputError("Неизвестный вариант.")
        filters = subscription.search_filters
        current: list[str] = getattr(filters, field)
        updated = filters.model_copy(update={field: toggle(current, value)})
        # Пустой опыт / формат работы = «любой»
        await self._save_filters(subscription, updated)

    async def set_name(self, subscription: Subscription, text: str) -> None:
        name = text.strip()[:MAX_NAME_LENGTH]
        if not name:
            raise InvalidInputError("Название не может быть пустым.")
        await self._repo.update_filters(subscription.id, name=name)

    async def set_exclude_words(self, subscription: Subscription, text: str) -> None:
        await self._repo.update_filters(subscription.id, exclude_words=parse_list(text))

    async def clear_excluded_employers(self, subscription: Subscription) -> None:
        await self._repo.update_filters(subscription.id, exclude_employers=[])

    async def set_catalog(self, subscription: Subscription, catalog: bool) -> None:
        await self._repo.set_catalog(subscription.id, catalog)

    async def set_active(self, subscription: Subscription, is_active: bool) -> None:
        await self._repo.set_active(subscription.id, is_active)

    async def delete(self, subscription: Subscription) -> None:
        await self._repo.delete(subscription.id)

    async def hide_employer(self, user_id: int, employer_id: str, *, hide: bool = True) -> int:
        """Скрыть (или вернуть) работодателя во всех подписках пользователя."""
        subscriptions = await self._repo.list_for_user(user_id)
        for subscription in subscriptions:
            if hide:
                await self._repo.add_excluded_employer(subscription.id, employer_id)
            else:
                await self._repo.remove_excluded_employer(subscription.id, employer_id)
        return len(subscriptions)
