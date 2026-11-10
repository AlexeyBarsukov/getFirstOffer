import json
from typing import Any

import httpx
import pytest

from app.apply.decision import decide
from app.apply.drafter import ANSWER_SCHEMA, MAX_FIXES, Drafter, VacancyBrief, build_user_prompt
from app.apply.letter_rules import RequiredPhrase
from app.apply.llm import ChatMessage, LLMError, OllamaLLM
from app.apply.profile import DEFAULT_PROFILE_PATH, CandidateProfile, Role, load_profile
from app.apply.vacancy_text import VacancyText
from tests.test_letter_rules import GOOD_LETTER

PROFILE = CandidateProfile.model_validate(
    {
        "name": "Алексей Барсуков",
        "telegram": "@Aleksey_Barsukov_development",
        "city": "Казань",
        "experience_since": 2021,
        "skills": ["React", "TypeScript", "MUI", "Jest", "React Testing Library"],
        "facts": ["Frontend-разработчик с 2021 года"],
        "do_not_claim": ["Angular"],
        "roles": {
            "frontend": {"title": "Frontend-разработчик", "min_salary": 160000, "angle": "стек"},
            "qa": {"title": "QA-инженер", "angle": "тесты", "facts": ["Писал тесты на Jest"]},
        },
    }
)
VACANCY = VacancyBrief(
    name="Frontend-разработчик",
    employer="INTERA",
    area="Казань",
    salary="от 200 000 ₽",
    work_format="Удалённо",
)


def answer(letter: str = GOOD_LETTER, role: str = "frontend", score: int = 82) -> str:
    return json.dumps(
        {
            "role": role,
            "score": score,
            "reasons": ["React и TypeScript совпадают"],
            "hook": "личный кабинет на React",
            "letter": letter,
        },
        ensure_ascii=False,
    )


class FakeLLM:
    """Отдаёт заготовленные ответы по очереди и запоминает диалоги."""

    name = "fake:1b"

    def __init__(self, *answers: str | Exception) -> None:
        self.answers = list(answers)
        self.calls: list[list[ChatMessage]] = []

    async def aclose(self) -> None:
        pass

    async def chat_json(
        self, messages: list[ChatMessage], schema: dict[str, Any], temperature: float
    ) -> str:
        assert schema == ANSWER_SCHEMA
        self.calls.append(list(messages))
        value = self.answers.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


async def test_clean_letter_in_one_call() -> None:
    llm = FakeLLM(answer(GOOD_LETTER.replace(", это ровно", " — это ровно")))

    draft = await Drafter(llm, PROFILE).draft(VACANCY, VacancyText(description="React"))

    assert len(llm.calls) == 1
    assert draft.role == Role.FRONTEND
    assert draft.score == 82
    assert draft.problems == []
    assert draft.attempts == 1
    assert draft.model == "fake:1b"
    # Длинное тире исправлено кодом, без перегенерации
    assert draft.letter == GOOD_LETTER


async def test_bad_letter_is_rewritten_with_problems_in_dialog() -> None:
    bad = GOOD_LETTER.replace("Готов обсудить", "С большим энтузиазмом готов обсудить")
    llm = FakeLLM(answer(bad), answer(GOOD_LETTER, score=10))

    draft = await Drafter(llm, PROFILE).draft(VACANCY, VacancyText(description="React"))

    assert draft.letter == GOOD_LETTER
    assert draft.problems == []
    assert draft.attempts == 2
    assert draft.history == [bad, GOOD_LETTER]
    # Оценка из первого ответа: при переписывании модель просили менять только письмо
    assert draft.score == 82
    fix_dialog = llm.calls[1]
    assert [m.role for m in fix_dialog] == ["system", "user", "assistant", "user"]
    assert fix_dialog[2].content == answer(bad)
    assert "«с большим энтузиазмом»" in fix_dialog[3].content


async def test_gives_up_after_max_fixes_and_keeps_best_version() -> None:
    one_problem = GOOD_LETTER + " 🚀"
    two_problems = one_problem.replace("Здравствуйте!", "Привет!")
    llm = FakeLLM(*(answer(two_problems), answer(one_problem), answer(two_problems)))

    draft = await Drafter(llm, PROFILE).draft(VACANCY, VacancyText())

    assert len(llm.calls) == 1 + MAX_FIXES
    assert draft.attempts == 1 + MAX_FIXES
    # Последняя версия хуже второй — остаётся вторая
    assert draft.letter == one_problem
    assert draft.problems == ["есть смайлики"]


async def test_unparsable_fix_is_skipped() -> None:
    bad = GOOD_LETTER + " 🚀"
    llm = FakeLLM(answer(bad), "не JSON", answer(GOOD_LETTER))

    draft = await Drafter(llm, PROFILE).draft(VACANCY, VacancyText())

    assert draft.letter == GOOD_LETTER
    assert draft.attempts == 3


async def test_other_role_is_not_rewritten() -> None:
    llm = FakeLLM(answer("Коротко", role="other", score=5))

    draft = await Drafter(llm, PROFILE).draft(VACANCY, VacancyText())

    assert len(llm.calls) == 1
    assert draft.role == Role.OTHER
    assert draft.problems


@pytest.mark.parametrize(
    "raw",
    [
        "не JSON",
        json.dumps({"role": "designer", "score": 50, "reasons": [], "hook": "", "letter": ""}),
        json.dumps({"role": "qa", "score": 150, "reasons": [], "hook": "", "letter": ""}),
    ],
)
async def test_first_answer_off_schema(raw: str) -> None:
    with pytest.raises(LLMError, match="не по схеме"):
        await Drafter(FakeLLM(raw), PROFILE).draft(VACANCY, VacancyText())


async def test_llm_error_propagates() -> None:
    with pytest.raises(LLMError):
        await Drafter(FakeLLM(LLMError("нет Ollama")), PROFILE).draft(VACANCY, VacancyText())


async def test_required_phrase_from_description_is_checked() -> None:
    text = VacancyText(description="В письме укажите слово «банан».")
    llm = FakeLLM(answer(GOOD_LETTER), answer(GOOD_LETTER + " Банан."))

    draft = await Drafter(llm, PROFILE).draft(VACANCY, text)

    assert draft.required_phrase == RequiredPhrase("банан")
    assert "«банан» дословно" in llm.calls[0][1].content
    assert draft.letter.endswith("Банан.")
    assert draft.problems == []


async def test_recent_letters_block_copies() -> None:
    llm = FakeLLM(*[answer(GOOD_LETTER)] * (1 + MAX_FIXES))

    draft = await Drafter(llm, PROFILE).draft(VACANCY, VacancyText(), recent=[GOOD_LETTER])

    assert draft.problems == ["слишком похоже на одно из прошлых писем"]


async def test_invented_experience_is_rewritten() -> None:
    # Настоящий случай из прогона: модель взяла стек из вакансии и выдала за опыт
    invented = GOOD_LETTER.replace(
        "Делал авторизацию", "Работаю с микрофронтендами и Module Federation. Делал авторизацию"
    )
    honest = GOOD_LETTER.replace(
        "Делал авторизацию",
        "С микрофронтендами не работал, но быстро разберусь. Делал авторизацию",
    )
    llm = FakeLLM(answer(invented), answer(honest))

    draft = await Drafter(llm, PROFILE).draft(VACANCY, VacancyText())

    assert draft.letter == honest
    assert draft.problems == []
    fix_request = llm.calls[1][-1].content
    assert "выдуман опыт, которого нет у кандидата: Module Federation, микрофронтенды" in (
        fix_request
    )


async def test_do_not_claim_and_years_from_profile() -> None:
    bad = GOOD_LETTER.replace("на MUI", "на Angular").replace("с 2021 года", "уже 8 лет")
    llm = FakeLLM(*[answer(bad)] * (1 + MAX_FIXES))

    draft = await Drafter(llm, PROFILE).draft(VACANCY, VacancyText())

    years = PROFILE.experience_years()
    assert draft.problems == [
        "выдуман опыт, которого нет у кандидата: Angular "
        "(можно только честно: «с этим не работал, но разберусь»)",
        f"неверный стаж: 8; у кандидата {years} лет опыта",
    ]


def test_prompt_has_years_and_skills() -> None:
    prompt = build_user_prompt(PROFILE, VACANCY, VacancyText(), None)

    years = PROFILE.experience_years()
    assert f"Стаж: {years} лет коммерческой разработки (с 2021 года)" in prompt
    assert "Навыки: React, TypeScript, MUI, Jest, React Testing Library" in prompt


def test_user_prompt_contains_facts_and_vacancy() -> None:
    text = VacancyText(description="Пишем на Vue", skills=["Vue", "Pinia"])

    prompt = build_user_prompt(PROFILE, VACANCY, text, RequiredPhrase("Привет", at_start=True))

    for part in (
        "Telegram: @Aleksey_Barsukov_development",
        "- Frontend-разработчик с 2021 года",
        "Если вакансия qa (QA-инженер): тесты",
        "  - Писал тесты на Jest",
        "Этого у кандидата НЕТ, не упоминай как опыт: Angular",
        "Компания: INTERA",
        "Формат: Удалённо",
        "Навыки: Vue, Pinia",
        "Пишем на Vue",
        "Начни письмо с «Привет» дословно.",
    ):
        assert part in prompt


def test_user_prompt_without_description() -> None:
    prompt = build_user_prompt(
        PROFILE, VACANCY.model_copy(update={"work_format": ""}), VacancyText(), None
    )

    assert "описания нет" in prompt
    assert "Формат:" not in prompt
    assert "Работодатель просит" not in prompt


@pytest.mark.parametrize(
    ("role", "score", "salary", "send", "reason"),
    [
        (Role.FRONTEND, 80, (200000, None, "RUR"), True, None),
        (Role.FRONTEND, 80, (None, None, None), True, None),
        (Role.FRONTEND, 80, (100000, 150000, "RUR"), False, "зарплата до 150000 ниже 160000"),
        (Role.FRONTEND, 80, (120000, None, "RUR"), False, "зарплата до 120000 ниже 160000"),
        (Role.FRONTEND, 80, (100000, 200000, "RUR"), True, None),
        # Валюта другая — не сравниваем с рублёвым порогом
        (Role.FRONTEND, 80, (3000, None, "USD"), True, None),
        (Role.QA, 60, (40000, 50000, "RUR"), True, None),
        (Role.FRONTEND, 49, (None, None, None), False, "оценка 49 ниже порога 50"),
        (Role.OTHER, 99, (None, None, None), False, "роль не из списка"),
        (Role.SUPPORT, 90, (None, None, None), False, "роль support не описана"),
    ],
)
def test_decide(
    role: Role,
    score: int,
    salary: tuple[int | None, int | None, str | None],
    send: bool,
    reason: str | None,
) -> None:
    decision = decide(PROFILE, role, score, 50, *salary)

    assert decision.send is send
    if reason is None:
        assert decision.reason is None
    else:
        assert decision.reason is not None and decision.reason.startswith(reason)


def test_repository_profile_is_valid() -> None:
    profile = load_profile(DEFAULT_PROFILE_PATH)

    assert set(profile.roles) == {Role.FRONTEND, Role.QA, Role.SUPPORT, Role.ANALYST}
    assert profile.facts


async def test_ollama_request_and_answer() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"role": "assistant", "content": '{"a":1}'}})

    llm = OllamaLLM("http://ollama", "qwen3:8b", transport=httpx.MockTransport(handler))
    try:
        raw = await llm.chat_json(
            [ChatMessage(role="user", content="привет")], {"type": "object"}, 0.3
        )
    finally:
        await llm.aclose()

    assert raw == '{"a":1}'
    [body] = requests
    assert body["model"] == "qwen3:8b"
    assert body["format"] == {"type": "object"}
    assert body["stream"] is False
    assert body["think"] is False
    assert body["options"]["temperature"] == 0.3
    assert body["messages"] == [{"role": "user", "content": "привет"}]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(404, text="model 'qwen3:8b' not found"), "Ollama ответила 404"),
        (httpx.ConnectError("refused"), "Ollama недоступна"),
    ],
)
async def test_ollama_errors(response: httpx.Response | Exception, message: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(response, Exception):
            raise response
        return response

    llm = OllamaLLM("http://ollama", "qwen3:8b", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(LLMError, match=message):
            await llm.chat_json([], {}, 0.7)
    finally:
        await llm.aclose()
