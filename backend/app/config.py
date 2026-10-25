from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env лежит в корне репозитория (на уровень выше backend/)
ROOT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["local", "test", "production"] = "local"
    log_level: str = "INFO"

    database_url: str = (
        "postgresql+asyncpg://getfirstoffer:getfirstoffer@localhost:5433/getfirstoffer"
    )
    redis_url: str = "redis://localhost:6380/0"

    hh_api_base_url: str = "https://api.hh.ru"
    hh_user_agent: str = Field(min_length=1)
    # Client ID / Secret с dev.hh.ru — нужны только чтобы один раз получить hh_app_token
    hh_client_id: str | None = None
    hh_client_secret: SecretStr | None = None
    hh_app_token: SecretStr | None = None
    # Минимальный интервал между запросами к api.hh.ru, секунды
    hh_min_request_interval: float = 0.5
    parser_enabled: bool = False

    telegram_bot_token: SecretStr | None = None
    # Куда слать алерты о проблемах сервиса (chat_id в Telegram)
    admin_chat_id: int | None = None

    smtp_host: str = "smtp.yandex.ru"
    smtp_port: int = 465
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None

    jwt_secret: SecretStr = SecretStr("change-me")


@lru_cache
def get_settings() -> Settings:
    return Settings()
