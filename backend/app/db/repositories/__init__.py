"""Доступ к данным. Репозитории не делают commit — транзакцией управляет вызывающий код."""

from app.db.repositories.drafts import DraftRepository
from app.db.repositories.events import EventRepository
from app.db.repositories.subscriptions import SubscriptionRepository
from app.db.repositories.users import UserRepository
from app.db.repositories.vacancies import VacancyRepository

__all__ = [
    "DraftRepository",
    "EventRepository",
    "SubscriptionRepository",
    "UserRepository",
    "VacancyRepository",
]
