"""Локальная модель через Ollama (2part.md, раздел 10.2).

Ответ ограничен JSON-схемой (параметр format), поэтому всегда разбирается в pydantic-модель.
"""

from typing import Any, Protocol

import httpx
from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: str
    content: str


class LLMError(Exception):
    pass


class LLM(Protocol):
    name: str

    async def aclose(self) -> None: ...

    async def chat_json(
        self, messages: list[ChatMessage], schema: dict[str, Any], temperature: float
    ) -> str:
        """JSON-строка по схеме."""
        ...


class OllamaLLM:
    def __init__(
        self,
        base_url: str,
        model: str,
        # Письмо на процессоре пишется минуты: даём запас
        timeout: float = 600.0,
        num_ctx: int = 8192,
        keep_alive: str = "60m",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = model
        self._model = model
        self._num_ctx = num_ctx
        self._keep_alive = keep_alive
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def chat_json(
        self, messages: list[ChatMessage], schema: dict[str, Any], temperature: float
    ) -> str:
        try:
            response = await self._http.post(
                "/api/chat",
                json={
                    "model": self._model,
                    "messages": [m.model_dump() for m in messages],
                    "format": schema,
                    "stream": False,
                    # Модель остаётся в памяти: иначе каждое письмо начинается с загрузки
                    "keep_alive": self._keep_alive,
                    # think: у «думающих» моделей рассуждения на процессоре удваивают время
                    "think": False,
                    "options": {"temperature": temperature, "num_ctx": self._num_ctx},
                },
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama недоступна: {exc!r}") from exc
        if response.is_error:
            raise LLMError(f"Ollama ответила {response.status_code}: {response.text[:300]}")
        content: str = response.json()["message"]["content"]
        return content
