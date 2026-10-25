from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bots.telegram.callbacks import CatalogCB, EditCB, PresetCB, SubCB, ToggleCB
from app.db.models import Subscription, Vacancy
from app.services.presets import PRESETS
from app.services.subscriptions import TOGGLE_FIELDS


def presets() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for preset in PRESETS.values():
        builder.button(text=preset.title, callback_data=PresetCB(key=preset.key))
    builder.adjust(2)
    return builder.as_markup()


def subscription_card(subscription: Subscription) -> InlineKeyboardMarkup:
    sid = subscription.id
    pause = (
        InlineKeyboardButton(text="⏸ Пауза", callback_data=SubCB(action="pause", id=sid).pack())
        if subscription.is_active
        else InlineKeyboardButton(
            text="▶️ Включить", callback_data=SubCB(action="resume", id=sid).pack()
        )
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Изменить", callback_data=SubCB(action="edit", id=sid).pack()
                ),
                pause,
                InlineKeyboardButton(
                    text="🗑 Удалить", callback_data=SubCB(action="delete", id=sid).pack()
                ),
            ]
        ]
    )


def edit_menu(subscription: Subscription) -> InlineKeyboardMarkup:
    sid = subscription.id
    builder = InlineKeyboardBuilder()
    fields = [
        ("🔤 Ключевые слова", "keywords"),
        ("👔 Роли", "professional_roles"),
        ("📍 Регион", "areas"),
        ("🏠 Формат работы", "work_format"),
        ("🎓 Опыт", "experience"),
        ("💰 Зарплата", "salary"),
        ("🚫 Стоп-слова", "exclude_words"),
        ("🏷 Название", "name"),
    ]
    for text, field in fields:
        builder.button(text=text, callback_data=EditCB(id=sid, field=field))
    builder.button(
        text="📚 Подборка: вкл" if subscription.catalog else "📚 Подборка: выкл",
        callback_data=EditCB(id=sid, field="catalog"),
    )
    if subscription.exclude_employers:
        builder.button(
            text=f"🏢 Вернуть скрытые компании ({len(subscription.exclude_employers)})",
            callback_data=EditCB(id=sid, field="clear_employers"),
        )
    builder.button(text="⬅️ Готово", callback_data=SubCB(action="show", id=sid))
    builder.adjust(2)
    return builder.as_markup()


def toggles(subscription: Subscription, field: str) -> InlineKeyboardMarkup:
    selected: list[str] = getattr(subscription.search_filters, field)
    builder = InlineKeyboardBuilder()
    for value, name in TOGGLE_FIELDS[field].items():
        mark = "✅" if value in selected else "▫️"
        builder.button(
            text=f"{mark} {name}",
            callback_data=ToggleCB(id=subscription.id, field=field, value=value),
        )
    builder.button(text="⬅️ Готово", callback_data=SubCB(action="edit", id=subscription.id))
    builder.adjust(2)
    return builder.as_markup()


def delete_confirm(subscription: Subscription) -> InlineKeyboardMarkup:
    sid = subscription.id
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Да, удалить", callback_data=SubCB(action="delete_yes", id=sid).pack()
                ),
                InlineKeyboardButton(
                    text="Отмена", callback_data=SubCB(action="show", id=sid).pack()
                ),
            ]
        ]
    )


def catalog_card(vacancy: Vacancy) -> InlineKeyboardMarkup:
    vid = vacancy.id
    rows = [
        [InlineKeyboardButton(text="↗️ Открыть и откликнуться", url=vacancy.url)],
        [
            InlineKeyboardButton(
                text="✅ Откликнулся",
                callback_data=CatalogCB(action="applied", vacancy_id=vid).pack(),
            ),
            InlineKeyboardButton(
                text="🚫 Не подходит",
                callback_data=CatalogCB(action="skipped", vacancy_id=vid).pack(),
            ),
        ],
        [
            InlineKeyboardButton(
                text="⏭ Позже", callback_data=CatalogCB(action="later", vacancy_id=vid).pack()
            ),
        ],
    ]
    if vacancy.employer_id:
        rows[2].append(
            InlineKeyboardButton(
                text="🏢 Скрыть компанию",
                callback_data=CatalogCB(action="hide", vacancy_id=vid).pack(),
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)
