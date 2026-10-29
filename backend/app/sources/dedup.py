"""Ключ дубля: одна и та же вакансия, опубликованная на нескольких площадках.

Компания часто размещает вакансию сразу на hh.ru и Хабре, но пишет по-разному:
«Frontend-разработчик (React)» и «Middle Frontend разработчик React», «ООО «Ромашка»» и
«Ромашка». Ключ — нормализованные компания + слова названия без грейдов, порядок слов
не важен. Город в ключ не входит: площадки пишут его по-разному (Хабр — все города
списком), поэтому дубли ищутся только между разными площадками (см. VacancyRepository).
"""

import hashlib
import re

# Грейды и формат работы — одна вакансия может называться с ними и без них
_NOISE_WORDS = frozenset(
    {
        "intern",
        "junior",
        "middle",
        "senior",
        "lead",
        "стажер",
        "младший",
        "старший",
        "ведущий",
        "главный",
        "remote",
        "удаленно",
        "удаленка",
        "удаленный",
        "удаленная",
    }
)
_LEGAL_FORMS = re.compile(r"^(ооо|оао|зао|пао|ао|ип|llc|ltd|inc|gmbh)\s+|\s+(llc|ltd|inc)$")
_NON_WORD = re.compile(r"[^\w]+")


def _normalize(text: str) -> str:
    return text.casefold().replace("ё", "е")


def company_name(name: str) -> str:
    """«ООО «Ромашка»» → «ромашка»."""
    text = " ".join(_NON_WORD.sub(" ", _normalize(name)).split())
    return _LEGAL_FORMS.sub("", text).strip()


def title_words(title: str) -> list[str]:
    words = _NON_WORD.sub(" ", _normalize(title)).split()
    return sorted({w for w in words if w not in _NOISE_WORDS and not w.isdigit()})


def dedup_key(employer_name: str, title: str) -> str | None:
    """Ключ дубля или None, если сравнивать не по чему (анонимная компания, пустое название)."""
    company = company_name(employer_name)
    words = title_words(title)
    if not company or not words:
        return None
    digest = hashlib.sha1(f"{company}|{' '.join(words)}".encode()).hexdigest()
    return digest[:32]
