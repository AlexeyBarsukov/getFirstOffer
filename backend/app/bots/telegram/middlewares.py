from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Chat, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ChannelType
from app.db.repositories import UserRepository

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class DbSessionMiddleware(BaseMiddleware):
    """Сессия БД и пользователь (по chat_id) для каждого события; commit после обработчика.

    Бот работает только в личных чатах — события из групп игнорируются.
    Фабрика сессий берётся из данных диспетчера: dispatcher["session_factory"].
    """

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        chat: Chat | None = data.get("event_chat")
        if chat is None or chat.type != ChatType.PRIVATE:
            return None
        session_factory: async_sessionmaker[AsyncSession] = data["session_factory"]
        async with session_factory() as session:
            user, channel = await UserRepository(session).get_or_create_by_channel(
                ChannelType.TELEGRAM, str(chat.id)
            )
            data["session"] = session
            data["user"] = user
            data["channel"] = channel
            result = await handler(event, data)
            await session.commit()
            return result
