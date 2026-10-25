import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin, str_enum
from app.sources.base import SearchFilters


class ChannelType(enum.StrEnum):
    TELEGRAM = "telegram"
    WEB = "web"


class VacancyStatus(enum.StrEnum):
    OPEN = "open"
    ARCHIVED = "archived"
    # Вакансия удалена с hh.ru (GET /vacancies/{id} вернул 404)
    CLOSED = "closed"


class SourceName(enum.StrEnum):
    API = "api"
    PARSER = "parser"


class EventType(enum.StrEnum):
    NEW = "new"
    ARCHIVED = "archived"
    CLOSED = "closed"
    SALARY_CHANGED = "salary_changed"
    CHANGED = "changed"


class DeliveryStatus(enum.StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class User(CreatedAtMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str | None] = mapped_column(String(320), unique=True)

    channels: Mapped[list["UserChannel"]] = relationship(back_populates="user")


class UserChannel(CreatedAtMixin, Base):
    """Куда слать уведомления и откуда пользователь управляет подписками."""

    __tablename__ = "user_channels"
    __table_args__ = (UniqueConstraint("type", "external_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    type: Mapped[ChannelType] = mapped_column(str_enum(ChannelType, "channel_type"))
    # chat_id в Telegram
    external_id: Mapped[str] = mapped_column(String(64))
    # false, если пользователь заблокировал бота
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))

    user: Mapped[User] = relationship(back_populates="channels")


class EmailLoginCode(CreatedAtMixin, Base):
    __tablename__ = "email_login_codes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    code_hash: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Subscription(CreatedAtMixin, Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    # SearchFilters.model_dump(): параметры поиска hh.ru
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB)
    exclude_words: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    # id работодателей hh.ru
    exclude_employers: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def search_filters(self) -> SearchFilters:
        return SearchFilters.model_validate(self.filters)


class Vacancy(Base):
    __tablename__ = "vacancies"

    # id вакансии на hh.ru
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    employer_id: Mapped[str | None] = mapped_column(String(32), index=True)
    employer_name: Mapped[str] = mapped_column(Text)
    area: Mapped[str] = mapped_column(Text)
    salary_from: Mapped[int | None] = mapped_column(Integer)
    salary_to: Mapped[int | None] = mapped_column(Integer)
    salary_currency: Mapped[str | None] = mapped_column(String(8))
    salary_gross: Mapped[bool | None] = mapped_column(Boolean)
    url: Mapped[str] = mapped_column(Text)
    status: Mapped[VacancyStatus] = mapped_column(
        str_enum(VacancyStatus, "vacancy_status"), server_default=VacancyStatus.OPEN.value
    )
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # Когда мы впервые увидели вакансию и откуда — для замера скорости источников
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    first_seen_source: Mapped[SourceName] = mapped_column(str_enum(SourceName, "source_name"))
    # Последняя проверка трекером изменений
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB)


class VacancySnapshot(Base):
    """Отслеживаемые поля вакансии на момент проверки — для поиска изменений."""

    __tablename__ = "vacancy_snapshots"
    __table_args__ = (Index("ix_vacancy_snapshots_vacancy_id_taken_at", "vacancy_id", "taken_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vacancy_id: Mapped[str] = mapped_column(ForeignKey("vacancies.id", ondelete="CASCADE"))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SubscriptionMatch(Base):
    __tablename__ = "subscription_matches"

    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), primary_key=True
    )
    vacancy_id: Mapped[str] = mapped_column(
        ForeignKey("vacancies.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    # Найдена при подгрузке за последние 24 часа, а не как свежая
    is_backfill: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    matched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class VacancyEvent(CreatedAtMixin, Base):
    """То, о чём сообщаем пользователю."""

    __tablename__ = "vacancy_events"
    __table_args__ = (
        # О новой вакансии сообщаем один раз, даже если она подошла под несколько подписок
        Index(
            "uq_vacancy_events_new_once",
            "user_id",
            "vacancy_id",
            unique=True,
            postgresql_where=text("type = 'new'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    vacancy_id: Mapped[str] = mapped_column(
        ForeignKey("vacancies.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[EventType] = mapped_column(str_enum(EventType, "event_type"))
    # Для изменений: {"field": {"old": ..., "new": ...}}
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")


class Delivery(Base):
    """Статус отправки события в конкретный канал."""

    __tablename__ = "deliveries"
    __table_args__ = (
        Index(
            "ix_deliveries_pending",
            "status",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    event_id: Mapped[int] = mapped_column(
        ForeignKey("vacancy_events.id", ondelete="CASCADE"), primary_key=True
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("user_channels.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[DeliveryStatus] = mapped_column(
        str_enum(DeliveryStatus, "delivery_status"), server_default=DeliveryStatus.PENDING.value
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
