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

    hh_user_agent: str = Field(min_length=1)
    hh_app_token: SecretStr | None = None
    parser_enabled: bool = False

    telegram_bot_token: SecretStr | None = None

    smtp_host: str = "smtp.yandex.ru"
    smtp_port: int = 465
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None

    jwt_secret: SecretStr = SecretStr("change-me")


@lru_cache
def get_settings() -> Settings:
    return Settings()
