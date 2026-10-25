"""Обработчики бота. Логика подписок — в app.services.subscriptions, здесь только диалог.

В data обработчиков (см. main.py и middlewares.py): session, user, areas, scheduler.
"""

from typing import Any, Protocol

import structlog
from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InaccessibleMessage, InlineKeyboardMarkup
from aiogram.types import Message as TgMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.telegram import keyboards, texts
from app.bots.telegram.callbacks import EditCB, HideEmployerCB, PresetCB, SubCB, ToggleCB
from app.db.models import Subscription, User
from app.notifiers.messages import hide_employer_button
from app.services.areas import AreaIndex
from app.services.subscriptions import TOGGLE_FIELDS, InvalidInputError, SubscriptionService

logger = structlog.get_logger(__name__)
router = Router(name="main")

# Поля, которые меняют поиск: после них — внеочередная проверка с подгрузкой за 24 часа
SEARCH_FIELDS = {"keywords", "areas", "salary", *TOGGLE_FIELDS}


class CheckScheduler(Protocol):
    async def schedule_check(self, subscription_id: int) -> None: ...


class EditInput(StatesGroup):
    waiting = State()


async def _edit_or_send(
    callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    """Обновить сообщение с кнопкой; если его уже нельзя изменить — прислать новое."""
    message = callback.message
    if message is None or isinstance(message, InaccessibleMessage):
        await callback.bot.send_message(callback.from_user.id, text, reply_markup=markup)  # type: ignore[union-attr]
        return
    await message.edit_text(text, reply_markup=markup)


async def _owned(
    callback: CallbackQuery, session: AsyncSession, user: User, subscription_id: int
) -> Subscription | None:
    subscription = await SubscriptionService(session).get_owned(user.id, subscription_id)
    if subscription is None:
        await callback.answer(texts.NOT_FOUND, show_alert=True)
    return subscription


async def _refresh(session: AsyncSession, subscription: Subscription) -> None:
    await session.flush()
    await session.refresh(subscription)


# --- Команды ---


@router.message(CommandStart())
async def cmd_start(
    message: TgMessage, session: AsyncSession, user: User, state: FSMContext
) -> None:
    await state.clear()
    if await SubscriptionService(session).list_for_user(user.id):
        await message.answer(texts.START_RETURNING)
    else:
        await message.answer(texts.START_NEW_USER, reply_markup=keyboards.presets())


@router.message(Command("help"))
async def cmd_help(message: TgMessage) -> None:
    await message.answer(texts.HELP)


@router.message(Command("new"))
async def cmd_new(message: TgMessage, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.CHOOSE_PRESET, reply_markup=keyboards.presets())


@router.message(Command("list"))
async def cmd_list(
    message: TgMessage, session: AsyncSession, user: User, areas: AreaIndex, state: FSMContext
) -> None:
    await state.clear()
    subscriptions = await SubscriptionService(session).list_for_user(user.id)
    if not subscriptions:
        await message.answer(texts.NO_SUBSCRIPTIONS)
        return
    for subscription in subscriptions:
        await message.answer(
            texts.describe_subscription(subscription, areas),
            reply_markup=keyboards.subscription_card(subscription),
        )


@router.message(Command("cancel"))
async def cmd_cancel(message: TgMessage, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer(texts.NOTHING_TO_CANCEL)
        return
    await state.clear()
    await message.answer(texts.CANCELLED)


# --- Создание подписки ---


@router.callback_query(PresetCB.filter())
async def on_preset(
    callback: CallbackQuery,
    callback_data: PresetCB,
    session: AsyncSession,
    user: User,
    areas: AreaIndex,
    scheduler: CheckScheduler,
) -> None:
    subscription = await SubscriptionService(session).create_from_preset(user.id, callback_data.key)
    await session.commit()  # воркер должен увидеть подписку до начала проверки
    await scheduler.schedule_check(subscription.id)
    await callback.answer()
    await _edit_or_send(
        callback,
        texts.CREATED + "\n\n" + texts.describe_subscription(subscription, areas),
        keyboards.subscription_card(subscription),
    )


# --- Карточка подписки ---


@router.callback_query(SubCB.filter())
async def on_subscription_action(
    callback: CallbackQuery,
    callback_data: SubCB,
    session: AsyncSession,
    user: User,
    areas: AreaIndex,
    state: FSMContext,
) -> None:
    subscription = await _owned(callback, session, user, callback_data.id)
    if subscription is None:
        return
    service = SubscriptionService(session)
    await state.clear()

    match callback_data.action:
        case "pause" | "resume":
            await service.set_active(subscription, callback_data.action == "resume")
            await _refresh(session, subscription)
        case "edit":
            await callback.answer()
            await _edit_or_send(
                callback,
                texts.describe_subscription(subscription, areas) + "\n\nЧто поменять?",
                keyboards.edit_menu(subscription),
            )
            return
        case "delete":
            await callback.answer()
            await _edit_or_send(
                callback,
                texts.DELETE_CONFIRM.format(name=subscription.name),
                keyboards.delete_confirm(subscription),
            )
            return
        case "delete_yes":
            name = subscription.name
            await service.delete(subscription)
            await callback.answer()
            await _edit_or_send(callback, texts.DELETED.format(name=name))
            return

    await callback.answer()
    await _edit_or_send(
        callback,
        texts.describe_subscription(subscription, areas),
        keyboards.subscription_card(subscription),
    )


# --- Изменение полей ---


@router.callback_query(EditCB.filter())
async def on_edit_field(
    callback: CallbackQuery,
    callback_data: EditCB,
    session: AsyncSession,
    user: User,
    areas: AreaIndex,
    state: FSMContext,
) -> None:
    subscription = await _owned(callback, session, user, callback_data.id)
    if subscription is None:
        return
    field = callback_data.field

    if field in TOGGLE_FIELDS:
        await callback.answer()
        await _edit_or_send(
            callback, texts.TOGGLE_TITLES[field], keyboards.toggles(subscription, field)
        )
        return
    if field == "clear_employers":
        await SubscriptionService(session).clear_excluded_employers(subscription)
        await _refresh(session, subscription)
        await callback.answer("Скрытые компании снова будут приходить")
        await _edit_or_send(
            callback,
            texts.describe_subscription(subscription, areas) + "\n\nЧто поменять?",
            keyboards.edit_menu(subscription),
        )
        return
    if field not in texts.PROMPTS:
        await callback.answer()
        return

    await state.set_state(EditInput.waiting)
    await state.update_data(subscription_id=subscription.id, field=field)
    await callback.answer()
    await callback.bot.send_message(  # type: ignore[union-attr]
        callback.from_user.id, texts.PROMPTS[field] + texts.PROMPT_FOOTER
    )


@router.message(StateFilter(EditInput.waiting), F.text)
async def on_edit_input(
    message: TgMessage,
    session: AsyncSession,
    user: User,
    areas: AreaIndex,
    state: FSMContext,
    scheduler: CheckScheduler,
) -> None:
    data: dict[str, Any] = await state.get_data()
    service = SubscriptionService(session)
    subscription = await service.get_owned(user.id, int(data["subscription_id"]))
    if subscription is None:
        await state.clear()
        await message.answer(texts.NOT_FOUND)
        return

    field: str = data["field"]
    text = message.text or ""
    try:
        match field:
            case "keywords":
                await service.set_keywords(subscription, text)
            case "areas":
                await service.set_areas(subscription, text, areas)
            case "salary":
                await service.set_salary(subscription, text)
            case "exclude_words":
                await service.set_exclude_words(subscription, text)
            case "name":
                await service.set_name(subscription, text)
    except InvalidInputError as exc:
        # Остаёмся в режиме ввода — можно сразу прислать исправленное значение
        await message.answer(f"⚠️ {exc}{texts.PROMPT_FOOTER}")
        return

    await state.clear()
    await _refresh(session, subscription)
    search_changed = field in SEARCH_FIELDS and subscription.last_checked_at is None
    if search_changed:
        await session.commit()
        await scheduler.schedule_check(subscription.id)
    await message.answer(
        (texts.FILTERS_CHANGED if search_changed else texts.SAVED)
        + "\n\n"
        + texts.describe_subscription(subscription, areas),
        reply_markup=keyboards.subscription_card(subscription),
    )


@router.callback_query(ToggleCB.filter())
async def on_toggle(
    callback: CallbackQuery,
    callback_data: ToggleCB,
    session: AsyncSession,
    user: User,
    scheduler: CheckScheduler,
) -> None:
    subscription = await _owned(callback, session, user, callback_data.id)
    if subscription is None:
        return
    try:
        await SubscriptionService(session).toggle_value(
            subscription, callback_data.field, callback_data.value
        )
    except InvalidInputError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await _refresh(session, subscription)
    await session.commit()
    await scheduler.schedule_check(subscription.id)
    await callback.answer()
    message = callback.message
    if isinstance(message, TgMessage):
        await message.edit_reply_markup(
            reply_markup=keyboards.toggles(subscription, callback_data.field)
        )


@router.message(StateFilter(EditInput.waiting))
async def on_edit_input_not_text(message: TgMessage) -> None:
    await message.answer("Пришли значение текстом." + texts.PROMPT_FOOTER)


# --- Кнопка под уведомлением ---


@router.callback_query(HideEmployerCB.filter())
async def on_hide_employer(
    callback: CallbackQuery, callback_data: HideEmployerCB, session: AsyncSession, user: User
) -> None:
    hide = not callback_data.undo
    count = await SubscriptionService(session).hide_employer(
        user.id, callback_data.employer_id, hide=hide
    )
    await callback.answer(
        f"Компания скрыта в подписках: {count}" if hide else "Компания снова будет приходить"
    )
    message = callback.message
    if isinstance(message, TgMessage) and message.reply_markup:
        # Меняем только нашу кнопку, «Открыть на hh.ru» остаётся
        swapped = hide_employer_button(callback_data.employer_id, undo=hide)
        rows = [
            [
                b.model_copy(update={"text": swapped.text, "callback_data": swapped.callback_data})
                if b.callback_data == callback.data
                else b
                for b in row
            ]
            for row in message.reply_markup.inline_keyboard
        ]
        await message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message()
async def fallback(message: TgMessage) -> None:
    await message.answer("Не понял 🙂 Список команд — /help")
