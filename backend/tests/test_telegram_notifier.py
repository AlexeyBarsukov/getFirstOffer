from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage

from app.notifiers.base import Button, ChannelUnavailableError, Message
from app.notifiers.telegram import TelegramNotifier
from tests.conftest import FakeSleep

METHOD = SendMessage(chat_id=1, text="x")
MESSAGE = Message(text="<b>Вакансия</b>", buttons=[Button("Открыть на hh.ru", "https://hh.ru/1")])


def make_notifier(fake_sleep: FakeSleep, side_effect: Any = None) -> tuple[TelegramNotifier, Any]:
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=side_effect)
    return TelegramNotifier(bot, per_chat_interval=0, sleep=fake_sleep), bot.send_message


async def test_sends_html_with_url_button(fake_sleep: FakeSleep) -> None:
    notifier, send = make_notifier(fake_sleep)

    await notifier.send("785137155", MESSAGE)

    kwargs = send.await_args.kwargs
    assert kwargs["chat_id"] == 785137155
    assert kwargs["text"] == "<b>Вакансия</b>"
    assert kwargs["parse_mode"] == "HTML"
    assert kwargs["link_preview_options"].is_disabled is True
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert (button.text, button.url) == ("Открыть на hh.ru", "https://hh.ru/1")


async def test_blocked_bot_is_channel_unavailable(fake_sleep: FakeSleep) -> None:
    error = TelegramForbiddenError(METHOD, "Forbidden: bot was blocked by the user")
    notifier, _ = make_notifier(fake_sleep, error)

    with pytest.raises(ChannelUnavailableError):
        await notifier.send("1", MESSAGE)


async def test_chat_not_found_is_channel_unavailable(fake_sleep: FakeSleep) -> None:
    notifier, _ = make_notifier(
        fake_sleep, TelegramBadRequest(METHOD, "Bad Request: chat not found")
    )

    with pytest.raises(ChannelUnavailableError):
        await notifier.send("1", MESSAGE)


async def test_other_bad_request_is_retryable(fake_sleep: FakeSleep) -> None:
    error = TelegramBadRequest(METHOD, "Bad Request: can't parse entities")
    notifier, _ = make_notifier(fake_sleep, error)

    with pytest.raises(TelegramBadRequest):
        await notifier.send("1", MESSAGE)


async def test_retry_after_waits_and_resends(fake_sleep: FakeSleep) -> None:
    notifier, send = make_notifier(
        fake_sleep, [TelegramRetryAfter(METHOD, "Too Many Requests", retry_after=3), None]
    )

    await notifier.send("1", MESSAGE)

    assert send.await_count == 2
    assert fake_sleep.calls == [3]
