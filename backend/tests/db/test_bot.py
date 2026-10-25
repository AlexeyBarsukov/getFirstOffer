"""Сквозные тесты бота: апдейты Telegram → диспетчер aiogram → обработчики → БД.

Запросы к Telegram API перехватывает MockedSession.
"""

from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bots.telegram import texts
from app.bots.telegram.callbacks import EditCB, HideEmployerCB, PresetCB, SubCB, ToggleCB
from app.bots.telegram.handlers import router
from app.bots.telegram.middlewares import DbSessionMiddleware
from app.db.models import ChannelType, Subscription
from app.db.repositories import SubscriptionRepository, UserRepository
from app.services.areas import AreaIndex
from app.services.presets import PRESETS

CHAT_ID = 785137155
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


class MockedSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109  # сигнатура BaseSession
    ) -> Any:
        self.requests.append(method)
        if isinstance(method, SendMessage | EditMessageText):
            chat = Chat(id=CHAT_ID, type="private")
            return Message(message_id=len(self.requests), date=NOW, chat=chat, text=method.text)
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109  # сигнатура BaseSession
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b""

    async def close(self) -> None:
        pass

    def sent(self, kind: type[TelegramMethod[Any]]) -> list[Any]:
        return [r for r in self.requests if isinstance(r, kind)]


class FakeScheduler:
    def __init__(self) -> None:
        self.checks: list[int] = []

    async def schedule_check(self, subscription_id: int) -> None:
        self.checks.append(subscription_id)


@pytest.fixture(scope="module")
def dispatcher() -> Dispatcher:
    # Роутер можно подключить к диспетчеру только один раз — диспетчер общий на модуль
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.include_router(router)
    return dp


class BotHarness:
    def __init__(self, dp: Dispatcher, session: AsyncSession) -> None:
        self.dp = dp
        self.session = session
        self.api = MockedSession()
        self.bot = Bot("42:TEST", session=self.api)
        self.scheduler = FakeScheduler()
        self.user = TgUser(id=CHAT_ID, is_bot=False, first_name="Алексей")
        self._update_id = 0
        dp["session_factory"] = async_sessionmaker(
            bind=session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        dp["areas"] = AreaIndex.fallback()
        dp["scheduler"] = self.scheduler

    def _next_id(self) -> int:
        self._update_id += 1
        return self._update_id

    def _message(self, text: str | None, chat_type: str = "private", markup: Any = None) -> Message:
        return Message(
            message_id=self._next_id(),
            date=NOW,
            chat=Chat(id=CHAT_ID, type=chat_type),
            from_user=self.user,
            text=text,
            reply_markup=markup,
        )

    async def send(self, text: str, chat_type: str = "private") -> None:
        update = Update(update_id=self._next_id(), message=self._message(text, chat_type))
        await self.dp.feed_update(self.bot, update)

    async def press(self, data: str, markup: InlineKeyboardMarkup | None = None) -> None:
        callback = CallbackQuery(
            id=str(self._next_id()),
            from_user=self.user,
            chat_instance="test",
            data=data,
            message=self._message("карточка", markup=markup),
        )
        await self.dp.feed_update(
            self.bot, Update(update_id=self._next_id(), callback_query=callback)
        )

    async def subscriptions(self) -> list[Subscription]:
        self.session.expire_all()
        channel = await UserRepository(self.session).get_channel(ChannelType.TELEGRAM, str(CHAT_ID))
        assert channel is not None
        return await SubscriptionRepository(self.session).list_for_user(channel.user_id)

    def last_text(self) -> str:
        texts_sent = [
            r.text for r in self.api.requests if isinstance(r, SendMessage | EditMessageText)
        ]
        return texts_sent[-1] or ""

    def alerts(self) -> list[str]:
        return [r.text for r in self.api.sent(AnswerCallbackQuery) if r.show_alert]


@pytest.fixture
async def harness(dispatcher: Dispatcher, session: AsyncSession) -> AsyncIterator[BotHarness]:
    h = BotHarness(dispatcher, session)
    yield h
    await dispatcher.storage.close()


async def test_start_offers_presets_then_create(harness: BotHarness) -> None:
    await harness.send("/start")

    [greeting] = harness.api.sent(SendMessage)
    assert greeting.text == texts.START_NEW_USER
    preset_buttons = [b.callback_data for row in greeting.reply_markup.inline_keyboard for b in row]
    assert PresetCB(key="frontend").pack() in preset_buttons

    await harness.press(PresetCB(key="frontend").pack())

    [subscription] = await harness.subscriptions()
    assert subscription.name == "Frontend"
    assert subscription.search_filters == PRESETS["frontend"].filters
    assert harness.scheduler.checks == [subscription.id]
    assert harness.last_text().startswith(texts.CREATED)

    await harness.send("/start")
    assert harness.last_text() == texts.START_RETURNING


async def test_edit_keywords_via_text_input(harness: BotHarness) -> None:
    await harness.press(PresetCB(key="support").pack())
    [subscription] = await harness.subscriptions()
    harness.scheduler.checks.clear()

    await harness.press(EditCB(id=subscription.id, field="keywords").pack())
    assert harness.last_text().startswith(texts.PROMPTS["keywords"])

    await harness.send("helpdesk, поддержка пользователей")

    [subscription] = await harness.subscriptions()
    assert subscription.search_filters.text == 'NAME:(helpdesk OR "поддержка пользователей")'
    assert harness.scheduler.checks == [subscription.id]
    assert harness.last_text().startswith(texts.FILTERS_CHANGED)


async def test_invalid_input_keeps_waiting(harness: BotHarness) -> None:
    await harness.press(PresetCB(key="frontend").pack())
    [subscription] = await harness.subscriptions()

    await harness.press(EditCB(id=subscription.id, field="salary").pack())
    await harness.send("много денег")
    assert harness.last_text().startswith("⚠️ Нужно число")

    await harness.send("100 000")
    [subscription] = await harness.subscriptions()
    assert subscription.search_filters.salary == 100000


async def test_cancel_stops_input(harness: BotHarness) -> None:
    await harness.press(PresetCB(key="frontend").pack())
    [subscription] = await harness.subscriptions()
    await harness.press(EditCB(id=subscription.id, field="name").pack())

    await harness.send("/cancel")
    await harness.send("Новое имя")

    [subscription] = await harness.subscriptions()
    assert subscription.name == "Frontend"
    assert harness.last_text() == "Не понял 🙂 Список команд — /help"


async def test_toggle_and_last_option_protection(harness: BotHarness) -> None:
    await harness.press(PresetCB(key="frontend").pack())
    [subscription] = await harness.subscriptions()
    sid = subscription.id

    await harness.press(ToggleCB(id=sid, field="work_format", value="HYBRID").pack())
    [subscription] = await harness.subscriptions()
    assert subscription.search_filters.work_format == ["REMOTE", "HYBRID"]
    assert harness.api.sent(EditMessageReplyMarkup)

    await harness.press(ToggleCB(id=sid, field="work_format", value="HYBRID").pack())
    await harness.press(ToggleCB(id=sid, field="work_format", value="REMOTE").pack())
    [subscription] = await harness.subscriptions()
    assert subscription.search_filters.work_format == ["REMOTE"]
    assert harness.alerts() == ["Оставь хотя бы один вариант."]


async def test_pause_and_delete(harness: BotHarness) -> None:
    await harness.press(PresetCB(key="frontend").pack())
    [subscription] = await harness.subscriptions()

    await harness.press(SubCB(action="pause", id=subscription.id).pack())
    [subscription] = await harness.subscriptions()
    assert subscription.is_active is False
    assert "⏸ на паузе" in harness.last_text()

    await harness.press(SubCB(action="delete", id=subscription.id).pack())
    assert harness.last_text().startswith("Удалить подписку «Frontend»?")
    await harness.press(SubCB(action="delete_yes", id=subscription.id).pack())
    assert await harness.subscriptions() == []


async def test_foreign_subscription_is_not_accessible(
    harness: BotHarness, session: AsyncSession
) -> None:
    other, _ = await UserRepository(session).get_or_create_by_channel(ChannelType.TELEGRAM, "1")
    foreign = await SubscriptionRepository(session).create(
        other.id, "Чужая", PRESETS["backend"].filters
    )
    await session.commit()

    await harness.press(SubCB(action="delete_yes", id=foreign.id).pack())

    assert harness.alerts() == [texts.NOT_FOUND]
    assert await session.get(Subscription, foreign.id) is not None


async def test_hide_employer_from_notification(harness: BotHarness) -> None:
    await harness.press(PresetCB(key="frontend").pack())
    await harness.press(PresetCB(key="backend").pack())
    hide = HideEmployerCB(employer_id="1740").pack()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть на hh.ru", url="https://hh.ru/vacancy/1")],
            [InlineKeyboardButton(text="🚫 Скрыть компанию", callback_data=hide)],
        ]
    )

    await harness.press(hide, markup)

    assert all(s.exclude_employers == ["1740"] for s in await harness.subscriptions())
    [edit] = harness.api.sent(EditMessageReplyMarkup)
    buttons = [b for row in edit.reply_markup.inline_keyboard for b in row]
    assert buttons[0].url == "https://hh.ru/vacancy/1"
    assert buttons[1].callback_data == HideEmployerCB(employer_id="1740", undo=True).pack()

    await harness.press(HideEmployerCB(employer_id="1740", undo=True).pack(), markup)
    assert all(s.exclude_employers == [] for s in await harness.subscriptions())


async def test_group_chats_are_ignored(harness: BotHarness) -> None:
    await harness.send("/start", chat_type="group")

    assert harness.api.requests == []
