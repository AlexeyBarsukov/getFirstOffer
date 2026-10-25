from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChannelType, User, UserChannel


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: int) -> User | None:
        return await self._session.get(User, user_id)

    async def get_channel(self, type_: ChannelType, external_id: str) -> UserChannel | None:
        return await self._session.scalar(
            select(UserChannel).where(
                UserChannel.type == type_, UserChannel.external_id == external_id
            )
        )

    async def get_or_create_by_channel(
        self, type_: ChannelType, external_id: str
    ) -> tuple[User, UserChannel]:
        """Пользователь по каналу (например, chat_id в Telegram). Создаёт, если его нет.

        Если канал был отключён (бот заблокирован), снова включает его.
        """
        channel = await self.get_channel(type_, external_id)
        if channel is not None:
            channel.is_active = True
            user = await self._session.get_one(User, channel.user_id)
            return user, channel

        user = User()
        channel = UserChannel(user=user, type=type_, external_id=external_id, is_active=True)
        self._session.add_all([user, channel])
        await self._session.flush()
        return user, channel

    async def deactivate_channel(self, channel_id: int) -> None:
        channel = await self._session.get_one(UserChannel, channel_id)
        channel.is_active = False

    async def active_channels(self, user_id: int) -> list[UserChannel]:
        result = await self._session.scalars(
            select(UserChannel)
            .where(UserChannel.user_id == user_id, UserChannel.is_active.is_(True))
            .order_by(UserChannel.id)
        )
        return list(result)

    async def set_email(self, user_id: int, email: str) -> None:
        user = await self._session.get_one(User, user_id)
        user.email = email.strip().lower()

    async def get_by_email(self, email: str) -> User | None:
        return await self._session.scalar(select(User).where(User.email == email.strip().lower()))
