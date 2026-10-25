import asyncio
from collections import defaultdict

import structlog
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from app.notifiers.base import ChannelUnavailableError, Message
from app.ratelimit import RateLimiter, Sleep

logger = structlog.get_logger(__name__)

# Telegram просит не чаще ~1 сообщения в секунду в один чат
PER_CHAT_INTERVAL = 1.0
# Ошибки BadRequest, после которых писать в чат бессмысленно
UNAVAILABLE_CHAT_ERRORS = ("chat not found", "user is deactivated", "bot was kicked")


class TelegramNotifier:
    def __init__(
        self,
        bot: Bot,
        per_chat_interval: float = PER_CHAT_INTERVAL,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._bot = bot
        self._sleep = sleep
        self._limiters: defaultdict[str, RateLimiter] = defaultdict(
            lambda: RateLimiter(per_chat_interval, sleep=sleep)
        )

    async def send(self, external_id: str, message: Message) -> None:
        await self._limiters[external_id].acquire()
        try:
            await self._send(external_id, message)
        except TelegramRetryAfter as exc:
            # Превысили лимит Telegram: ждём сколько просят и пробуем ещё раз
            logger.warning("telegram_retry_after", chat_id=external_id, retry_after=exc.retry_after)
            await self._sleep(exc.retry_after)
            await self._send(external_id, message)

    async def _send(self, external_id: str, message: Message) -> None:
        markup = None
        if message.buttons:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=b.text, url=b.url, callback_data=b.callback_data)]
                    for b in message.buttons
                ]
            )
        try:
            await self._bot.send_message(
                chat_id=int(external_id),
                text=message.text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramForbiddenError as exc:
            raise ChannelUnavailableError(exc.message) from exc
        except TelegramBadRequest as exc:
            if any(reason in exc.message.lower() for reason in UNAVAILABLE_CHAT_ERRORS):
                raise ChannelUnavailableError(exc.message) from exc
            raise
