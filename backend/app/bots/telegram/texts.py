from html import escape

from app.db.models import Subscription
from app.services.areas import AreaIndex
from app.services.presets import EXPERIENCE_NAMES, IT_ROLE_NAMES, WORK_FORMAT_NAMES
from app.sources.base import SearchFilters

HELP = (
    "Я присылаю новые вакансии с hh.ru, как только они появляются — "
    "обычно в течение пары минут.\n\n"
    "/new — новая подписка (готовые наборы: Frontend, Backend, Техподдержка, IT в целом)\n"
    "/list — мои подписки: изменить фильтры, поставить на паузу, удалить\n"
    "/cancel — отменить ввод\n"
    "/help — эта справка\n\n"
    "Под каждой вакансией есть кнопка «🚫 Скрыть компанию» — вакансии этого работодателя "
    "больше не придут."
)

START_NEW_USER = (
    "Привет! 👋\n\n" + HELP + "\n\nДавай создадим первую подписку — выбери набор фильтров:"
)
START_RETURNING = "С возвращением! 👋\n\n" + HELP

CHOOSE_PRESET = "Выбери набор фильтров — потом любой параметр можно поменять:"
NO_SUBSCRIPTIONS = "Подписок пока нет. Создай первую: /new"
CREATED = (
    "✅ Подписка создана. Сейчас пришлю вакансии за последние 24 часа, "
    "дальше — новые, как только появятся."
)
DELETE_CONFIRM = "Удалить подписку «{name}»? Найденные по ней вакансии больше не будут приходить."
DELETED = "🗑 Подписка «{name}» удалена."
NOT_FOUND = "Подписка не найдена — возможно, её уже удалили."
CANCELLED = "Отменил."
NOTHING_TO_CANCEL = "Нечего отменять."
FILTERS_CHANGED = "✅ Сохранил. Проверяю вакансии за последние 24 часа по новым фильтрам."
SAVED = "✅ Сохранил."

PROMPTS = {
    "keywords": (
        "Пришли ключевые слова через запятую — ищу их в названии вакансии:\n"
        "<code>react, vue, typescript</code>\n\n"
        "Можно запрос на языке hh.ru: <code>NAME:(react OR vue) NOT angular</code>\n"
        "«-» — без ключевых слов (тогда нужна хотя бы одна роль)."
    ),
    "areas": (
        "Пришли регионы через запятую, как на hh.ru:\n<code>Россия</code> или "
        "<code>Москва, Санкт-Петербург</code>"
    ),
    "salary": "Минимальная зарплата числом, например <code>80000</code>. «0» — не важна.",
    "exclude_words": (
        "Стоп-слова через запятую — вакансии с ними в названии не придут:\n"
        "<code>senior, lead, 1С</code>\n«-» — без стоп-слов."
    ),
    "name": "Новое название подписки:",
}
PROMPT_FOOTER = "\n\n/cancel — отменить"

TOGGLE_TITLES = {
    "professional_roles": "👔 Роли — отметь нужные:",
    "experience": "🎓 Опыт работы — отметь подходящие:",
    "work_format": "🏠 Формат работы — отметь подходящие:",
}


def _names(values: list[str], names: dict[str, str]) -> str:
    return ", ".join(names.get(v, v) for v in values) or "—"


def describe_filters(
    name: str,
    filters: SearchFilters,
    exclude_words: list[str],
    hidden_employers: int,
    areas: AreaIndex,
    is_active: bool | None = None,
) -> str:
    status = ""
    if is_active is not None:
        status = " — ✅ активна" if is_active else " — ⏸ на паузе"
    keywords = f"<code>{escape(filters.text)}</code>" if filters.text else "—"
    salary = f"от {filters.salary:,}".replace(",", " ") + " ₽" if filters.salary else "не важна"
    lines = [
        f"🔎 <b>{escape(name)}</b>{status}",
        "",
        f"🔤 Ключевые слова: {keywords}",
        f"👔 Роли: {escape(_names(filters.professional_roles, IT_ROLE_NAMES))}",
        f"📍 Регион: {escape(', '.join(areas.name(a) for a in filters.areas) or '—')}",
        f"🏠 Формат: {escape(_names(filters.work_format, WORK_FORMAT_NAMES))}",
        f"🎓 Опыт: {escape(_names(filters.experience, EXPERIENCE_NAMES))}",
        f"💰 Зарплата: {salary}",
        f"🚫 Стоп-слова: {escape(', '.join(exclude_words) or '—')}",
    ]
    if hidden_employers:
        lines.append(f"🏢 Скрыто компаний: {hidden_employers}")
    return "\n".join(lines)


def describe_subscription(subscription: Subscription, areas: AreaIndex) -> str:
    return describe_filters(
        subscription.name,
        subscription.search_filters,
        subscription.exclude_words,
        len(subscription.exclude_employers),
        areas,
        is_active=subscription.is_active,
    )
