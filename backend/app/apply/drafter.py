"""Анализ вакансии и сопроводительное письмо одним запросом к модели.

Один запрос, а не два: на процессоре разбор длинного промпта занимает столько же, сколько
сам ответ. Если письмо не прошло проверку (letter_rules), модель получает список нарушений
и переписывает письмо в том же диалоге: Ollama переиспользует уже разобранный промпт.
"""

import json
import time
from dataclasses import dataclass, field
from typing import Literal

import structlog
from pydantic import BaseModel, Field, ValidationError

from app.apply.letter_rules import (
    RequiredPhrase,
    check_letter,
    clean_letter,
    find_required_phrase,
)
from app.apply.llm import LLM, ChatMessage, LLMError
from app.apply.profile import ROLE_TITLES, CandidateProfile, Role
from app.apply.vacancy_text import VacancyText

logger = structlog.get_logger(__name__)

# Попыток переписать письмо после первой
MAX_FIXES = 2


class VacancyBrief(BaseModel):
    """Что нужно о вакансии, кроме текста."""

    name: str
    employer: str
    area: str
    salary: str
    work_format: str = ""


class ModelAnswer(BaseModel):
    role: Literal["frontend", "qa", "support", "analyst", "other"]
    score: int = Field(ge=0, le=100)
    reasons: list[str] = Field(max_length=4)
    hook: str
    letter: str


ANSWER_SCHEMA = ModelAnswer.model_json_schema()


@dataclass
class Draft:
    role: Role
    score: int
    reasons: list[str]
    hook: str
    letter: str
    # Нарушения, которые остались после всех попыток (пусто — письмо чистое)
    problems: list[str]
    required_phrase: RequiredPhrase | None
    attempts: int
    seconds: float
    model: str
    history: list[str] = field(default_factory=list)


SYSTEM_PROMPT = """\
Ты помогаешь кандидату откликаться на вакансии. По вакансии и фактам о кандидате ты:
1. определяешь роль вакансии: frontend (фронтенд-разработка, в том числе fullstack с упором \
на фронтенд), qa (тестирование), support (техническая поддержка), analyst (аналитик данных, \
BI, продуктовый или системный аналитик с SQL), other (всё остальное: backend, мобильная \
разработка без React Native, дизайн, менеджмент, продажи);
2. оцениваешь от 0 до 100, насколько кандидат подходит (стек, задачи, уровень);
3. коротко пишешь до 3 причин оценки. Технологии из вакансии, которых нет в навыках \
кандидата, снижают оценку;
4. находишь «крючок»: что делает компания или команда (продукт, задача, пользователи). \
Крючок описывает ВАКАНСИЮ, а не опыт кандидата;
5. пишешь сопроводительное письмо от имени кандидата.

Главное правило: о кандидате пиши ТОЛЬКО то, что есть в разделе «Факты о кандидате». \
Своим опытом можно называть только технологии из строки «Навыки». Технологии из вакансии \
(например Vue, Next.js, микрофронтенды, Docker) кандидату НЕ принадлежат, даже если \
вакансия их требует. Нельзя: «Работаю с микрофронтендами», «Имею опыт с Vue». Можно: \
«С Vue не работал, но на React делал похожие интерфейсы и быстро разберусь». Стаж бери \
только из строки «Стаж», не придумывай компании, проекты и цифры.

Правила письма:
- 400-900 символов, 4-6 предложений, 2-3 коротких абзаца, без списков и заголовков;
- первая строка отдельно: «Здравствуйте!»;
- первая фраза после приветствия про крючок: что в задачах компании перекликается \
с фактами о кандидате. Не начинай с «Меня заинтересовала вакансия»;
- пиши просто и по делу, как живой человек пишет коротко рекрутеру. Без смайликов, без \
длинных тире (используй запятые и точки), без восторгов и канцелярита. Запрещены фразы: \
«с большим энтузиазмом», «идеально подхожу», «динамичная команда», «внести вклад», \
«уверен, что смогу», «рад возможности», «с нетерпением жду», «кроме того», «более того»;
- если работодатель просит указать в письме слово или ответить на вопрос, сделай это \
в первой фразе после приветствия;
- предпоследний абзац: предложение созвониться и Telegram кандидата;
- последняя строка отдельно: только имя и фамилия кандидата.
Отвечай строго JSON по схеме, на русском языке."""


def _facts_block(profile: CandidateProfile) -> str:
    years = profile.experience_years()
    lines = [
        f"Имя: {profile.name}",
        f"Город: {profile.city}",
        f"Telegram: {profile.telegram}",
        f"Стаж: {years} лет коммерческой разработки (с {profile.experience_since} года)",
    ]
    if profile.skills:
        lines.append("Навыки: " + ", ".join(profile.skills))
    lines += [f"- {fact}" for fact in profile.facts]
    for role, data in profile.roles.items():
        lines.append(f"Если вакансия {role.value} ({data.title}): {data.angle}")
        lines += [f"  - {fact}" for fact in data.facts]
    if profile.do_not_claim:
        lines.append(
            "Этого у кандидата НЕТ, не упоминай как опыт: " + ", ".join(profile.do_not_claim)
        )
    return "\n".join(lines)


def build_user_prompt(
    profile: CandidateProfile,
    vacancy: VacancyBrief,
    text: VacancyText,
    required: RequiredPhrase | None,
) -> str:
    parts = [
        "## Факты о кандидате",
        _facts_block(profile),
        "",
        "## Вакансия",
        f"Название: {vacancy.name}",
        f"Компания: {vacancy.employer}",
        f"Город: {vacancy.area}",
        f"Зарплата: {vacancy.salary}",
    ]
    if vacancy.work_format:
        parts.append(f"Формат: {vacancy.work_format}")
    if text.skills:
        parts.append("Навыки: " + ", ".join(text.skills))
    parts += ["Описание:", text.description or "(описания нет, ориентируйся на название)"]
    if required:
        where = "Начни письмо с" if required.at_start else "Обязательно используй в письме"
        parts += ["", f"Работодатель просит: {where} «{required.text}» дословно."]
    return "\n".join(parts)


def _parse(raw: str) -> ModelAnswer:
    try:
        return ModelAnswer.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise LLMError(f"модель ответила не по схеме: {exc}") from exc


class Drafter:
    def __init__(self, llm: LLM, profile: CandidateProfile, temperature: float = 0.7) -> None:
        self._llm = llm
        self._profile = profile
        self._temperature = temperature

    def _check(self, letter: str, required: RequiredPhrase | None, recent: list[str]) -> list[str]:
        profile = self._profile
        return check_letter(
            letter,
            required_phrase=required,
            recent=recent,
            forbidden_facts=profile.do_not_claim,
            known=profile.known_text(),
            years=profile.experience_years(),
            name=profile.name,
            telegram=profile.telegram,
        )

    async def draft(
        self, vacancy: VacancyBrief, text: VacancyText, recent: list[str] | None = None
    ) -> Draft:
        """recent — последние письма: новое не должно быть их копией."""
        recent = recent or []
        started = time.monotonic()
        required = find_required_phrase(text.description)
        messages = [
            ChatMessage(role="system", content=SYSTEM_PROMPT),
            ChatMessage(
                role="user", content=build_user_prompt(self._profile, vacancy, text, required)
            ),
        ]
        raw = await self._llm.chat_json(messages, ANSWER_SCHEMA, self._temperature)
        answer = _parse(raw)
        letter = clean_letter(answer.letter)
        problems = self._check(letter, required, recent)
        history = [letter]
        attempts = 1
        # Не тратим минуты процессора на письма для вакансий, которые всё равно пропустим
        while problems and attempts <= MAX_FIXES and answer.role != Role.OTHER.value:
            messages += [
                ChatMessage(role="assistant", content=raw),
                ChatMessage(
                    role="user",
                    content="Перепиши письмо, исправив: "
                    + "; ".join(problems)
                    + ". Остальные поля оставь как есть. Ответ тем же JSON.",
                ),
            ]
            raw = await self._llm.chat_json(messages, ANSWER_SCHEMA, self._temperature)
            attempts += 1
            try:
                fixed = _parse(raw)
            except LLMError as exc:
                logger.warning("draft_fix_unparsable", error=str(exc))
                continue
            candidate = clean_letter(fixed.letter)
            history.append(candidate)
            candidate_problems = self._check(candidate, required, recent)
            # Новая версия может оказаться хуже: оставляем ту, где меньше нарушений
            if len(candidate_problems) <= len(problems):
                letter, problems = candidate, candidate_problems
        role = Role(answer.role)
        logger.info(
            "draft_ready",
            vacancy=vacancy.name,
            role=role,
            score=answer.score,
            attempts=attempts,
            problems=len(problems),
            seconds=round(time.monotonic() - started, 1),
        )
        return Draft(
            role=role,
            score=answer.score,
            reasons=answer.reasons,
            hook=answer.hook,
            letter=letter,
            problems=problems,
            required_phrase=required,
            attempts=attempts,
            seconds=time.monotonic() - started,
            model=self._llm.name,
            history=history,
        )


def role_title(role: Role) -> str:
    return ROLE_TITLES[role]
