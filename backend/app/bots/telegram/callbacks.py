"""Данные inline-кнопок (Telegram ограничивает callback_data 64 байтами)."""

from aiogram.filters.callback_data import CallbackData


class PresetCB(CallbackData, prefix="preset"):
    key: str


class SubCB(CallbackData, prefix="sub"):
    """Действия с карточкой подписки: show, edit, pause, resume, delete, delete_yes."""

    action: str
    id: int


class EditCB(CallbackData, prefix="edit"):
    """Выбор поля для изменения: keywords, name, areas, salary, exclude_words,
    clear_employers или поле-переключатель (professional_roles, experience, work_format)."""

    id: int
    field: str


class ToggleCB(CallbackData, prefix="tg"):
    id: int
    field: str
    value: str


class HideEmployerCB(CallbackData, prefix="hide"):
    employer_id: str
    undo: bool = False
