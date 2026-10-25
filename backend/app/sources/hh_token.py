"""Однократное получение токена приложения hh.ru.

Запуск: cd backend && .venv/bin/python -m app.sources.hh_token
Нужны HH_CLIENT_ID и HH_CLIENT_SECRET в .env. Полученный токен вставить в HH_APP_TOKEN.
"""

import asyncio
import sys

import httpx

from app.config import get_settings


async def fetch_app_token(
    client_id: str,
    client_secret: str,
    user_agent: str,
    base_url: str = "https://api.hh.ru",
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    async with httpx.AsyncClient(
        base_url=base_url,
        headers={"HH-User-Agent": user_agent, "User-Agent": user_agent},
        timeout=10.0,
        transport=transport,
    ) as http:
        response = await http.post(
            "/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
    if response.is_error:
        raise RuntimeError(f"hh.ru вернул {response.status_code}: {response.text}")
    token: str = response.json()["access_token"]
    return token


def main() -> None:
    settings = get_settings()
    if not settings.hh_client_id or not settings.hh_client_secret:
        sys.exit("Заполни HH_CLIENT_ID и HH_CLIENT_SECRET в .env")
    token = asyncio.run(
        fetch_app_token(
            settings.hh_client_id,
            settings.hh_client_secret.get_secret_value(),
            settings.hh_user_agent,
            settings.hh_api_base_url,
        )
    )
    print(f"Вставь в .env:\nHH_APP_TOKEN={token}")


if __name__ == "__main__":
    main()
