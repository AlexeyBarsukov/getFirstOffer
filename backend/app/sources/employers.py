import hashlib


def employer_key(name: str) -> str:
    """Стабильный ключ работодателя по названию — одинаковый для API, RSS и страницы поиска.

    В RSS настоящего id работодателя нет, поэтому «Скрыть компанию» работает по этому ключу.
    Короткий, чтобы поместиться в callback_data кнопки (лимит Telegram — 64 байта).
    """
    digest = hashlib.sha1(name.strip().casefold().encode()).hexdigest()[:12]
    return f"n{digest}"
