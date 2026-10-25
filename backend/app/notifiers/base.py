from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Button:
    """Кнопка под сообщением: ссылка (url) или действие в боте (callback_data)."""

    text: str
    url: str | None = None
    callback_data: str | None = None


@dataclass(frozen=True)
class Message:
    # Разметка — HTML в варианте Telegram (<b>, <i>, <a>)
    text: str
    buttons: list[Button] = field(default_factory=list)


class ChannelUnavailableError(Exception):
    """В канал больше нельзя писать (бот заблокирован, чат удалён). Повторять бессмысленно."""


class Notifier(Protocol):
    async def send(self, external_id: str, message: Message) -> None:
        """Отправляет сообщение.

        ChannelUnavailableError — писать в канал больше нельзя, любая другая ошибка — повторить.
        """
        ...
