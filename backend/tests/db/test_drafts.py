"""Черновики откликов: очередь, воркер prepare_drafts, кнопки карточки и /drafts."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageReplyMarkup, SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.apply.llm import LLMError
from app.apply.vacancy_text import VacancyText
from app.bots.telegram.callbacks import DraftCB
from app.bots.telegram.drafts import router as drafts_router
from app.bots.telegram.middlewares import DbSessionMiddleware
from app.db.models import (
    ApplicationDraft,
    DraftStatus,
    MarkStatus,
    Vacancy,
    VacancyMark,
    VacancyStatus,
)
from app.db.repositories import DraftRepository
from app.workers.drafts import DRAFTS_LOCK_KEY, DraftsContext, prepare_drafts
from tests.db.test_bot import CHAT_ID, BotHarness, FakeScheduler
from tests.db.test_notifier import FakeNotifier, make_ctx, prepare
from tests.test_drafter import PROFILE, FakeLLM, answer
from tests.test_letter_rules import GOOD_LETTER

LONG_AGO = datetime.now(UTC) - timedelta(days=2)


class FakeTextSource:
    def __init__(self, errors: dict[str, Exception] | None = None) -> None:
        self.errors = errors or {}
        self.requested: list[str] = []

    async def aclose(self) -> None:
        pass

    async def get_text(self, vacancy_id: str, url: str, raw: dict[str, Any]) -> VacancyText:
        self.requested.append(vacancy_id)
        if error := self.errors.get(vacancy_id):
            raise error
        return VacancyText(description="React, TypeScript", letter_required=True)


LETTERS = [
    GOOD_LETTER,
    "Здравствуйте!\n"
    "Пишу по поводу тестировщика: как разработчик я много лет сам покрывал свой код тестами "
    "на Jest и React Testing Library и хорошо знаю, где обычно прячутся ошибки. Разбирал баги "
    "вместе с QA, воспроизводил их по шагам и проверял краевые случаи до релиза. Хочу перейти "
    "в тестирование и делать это системно. Если интересно, напишите в Telegram "
    "@Aleksey_Barsukov_development, отвечу быстро.\n"
    "Алексей",
    "Здравствуйте!\n"
    "Вижу, что у вас поддержка второй линии для SaaS-продукта. Много лет общался с "
    "заказчиками напрямую: выяснял, что именно сломалось, воспроизводил проблему у себя и "
    "доводил исправление до пользователя. Читаю логи, умею пользоваться DevTools и спокойно "
    "объясняю технические вещи простыми словами. Живу в Казани, могу начать быстро. Связь в "
    "Telegram @Aleksey_Barsukov_development.\n"
    "Алексей Барсуков",
]


def letter(n: int) -> str:
    """Разные письма: похожие не прошли бы проверку на повторы."""
    return LETTERS[n]


def drafts_ctx(
    session: AsyncSession,
    llm: FakeLLM,
    notifier: FakeNotifier | None = None,
    text_source: FakeTextSource | None = None,
) -> dict[str, Any]:
    ctx = make_ctx(session, notifier or FakeNotifier())
    ctx["drafts"] = DraftsContext(
        llm=llm,
        text_source=text_source or FakeTextSource(),
        profile=PROFILE,
        chat_id=CHAT_ID,
        min_score=50,
    )
    return ctx


async def drafts(session: AsyncSession) -> dict[str, ApplicationDraft]:
    session.expire_all()
    result = await session.scalars(select(ApplicationDraft))
    return {d.vacancy_id: d for d in result}


async def test_worker_drafts_newest_first_and_sends_only_ready(session: AsyncSession) -> None:
    # make_vacancy в prepare публикует вакансии по порядку: "3" самая свежая
    await prepare(session, "1", "2", "3")
    await session.commit()
    llm = FakeLLM(answer(letter(0)), answer(letter(1), score=20), answer(letter(2)))
    notifier = FakeNotifier()
    text_source = FakeTextSource()
    ctx = drafts_ctx(session, llm, notifier, text_source)

    stats = await prepare_drafts(ctx)

    assert stats == {"queued": 3, "ready": 2, "filtered": 1}
    by_vacancy = await drafts(session)
    assert by_vacancy["2"].status == DraftStatus.FILTERED
    assert by_vacancy["2"].filter_reason == "оценка 20 ниже порога 50"
    assert by_vacancy["2"].sent_at is None
    ready = by_vacancy["3"]
    assert ready.status == DraftStatus.READY
    assert ready.letter == letter(0)
    assert ready.role == "frontend"
    assert ready.score == 82
    assert ready.letter_required is True
    assert ready.model == "fake:1b"
    assert ready.sent_at is not None
    # Ни одна вакансия не обработана дважды, и следующий запуск ничего не делает
    assert sorted(text_source.requested) == ["1", "2", "3"]
    assert await prepare_drafts(ctx) == {}
    assert ctx["redis"].held == set()

    [(chat, card), _] = notifier.sent
    assert chat == str(CHAT_ID)
    assert card.text.startswith("📝 <b>Черновик отклика</b> · Frontend · 82/100")
    assert "✉️ Письмо обязательно" in card.text
    assert "<pre>Здравствуйте!" in card.text
    assert [b.text for b in card.buttons] == [
        "↗️ Открыть на hh.ru",
        "✅ Откликнулся",
        "🔁 Переписать",
        "✖️ Пропустить",
    ]
    assert card.buttons[0].url == "https://hh.ru/vacancy/3"
    assert card.buttons[1].callback_data == DraftCB(action="applied", id=ready.id).pack()


async def test_new_letter_is_checked_against_previous(session: AsyncSession) -> None:
    await prepare(session, "1", "2")
    await session.commit()
    # Модель упорно пишет одно и то же
    llm = FakeLLM(*[answer(letter(0))] * 4)

    await prepare_drafts(drafts_ctx(session, llm))

    by_vacancy = await drafts(session)
    assert by_vacancy["2"].problems == []
    assert by_vacancy["1"].problems == ["слишком похоже на одно из прошлых писем"]
    assert by_vacancy["1"].attempts == 3


async def test_queue_skips_old_closed_and_duplicate_vacancies(session: AsyncSession) -> None:
    user_id, _ = await prepare(session, "fresh", "closed", "dup", "old")
    await session.flush()
    closed = await session.get_one(Vacancy, "closed")
    closed.status = VacancyStatus.ARCHIVED
    dup = await session.get_one(Vacancy, "dup")
    dup.duplicate_of = "fresh"
    await session.flush()
    repo = DraftRepository(session)

    assert await repo.queue_new_vacancies(user_id, LONG_AGO) == 2
    assert await repo.queue_new_vacancies(user_id, LONG_AGO) == 0
    assert set(await drafts(session)) == {"fresh", "old"}
    # События NEW созданы сейчас — окно «с завтра» их не видит
    assert await repo.queue_new_vacancies(user_id, datetime.now(UTC) + timedelta(days=1)) == 0


async def test_llm_unavailable_leaves_queue_pending(session: AsyncSession) -> None:
    await prepare(session, "1", "2")
    await session.commit()
    llm = FakeLLM(LLMError("Ollama недоступна"))

    stats = await prepare_drafts(drafts_ctx(session, llm))

    assert stats == {"queued": 2}
    assert {d.status for d in (await drafts(session)).values()} == {DraftStatus.PENDING}
    assert len(llm.calls) == 1


async def test_broken_vacancy_fails_and_others_continue(session: AsyncSession) -> None:
    await prepare(session, "1", "2")
    await session.commit()
    text_source = FakeTextSource({"2": ValueError("на странице нет HH-Lux-InitialState")})
    llm = FakeLLM(answer(letter(0)))

    stats = await prepare_drafts(drafts_ctx(session, llm, text_source=text_source))

    assert stats == {"queued": 2, "failed": 1, "ready": 1}
    by_vacancy = await drafts(session)
    assert by_vacancy["2"].status == DraftStatus.FAILED
    assert "HH-Lux-InitialState" in (by_vacancy["2"].error or "")
    assert by_vacancy["1"].status == DraftStatus.READY


async def test_rewrite_sends_card_even_if_filtered_and_avoids_old_letter(
    session: AsyncSession,
) -> None:
    await prepare(session, "1")
    await session.commit()
    notifier = FakeNotifier()
    ctx = drafts_ctx(session, FakeLLM(answer(letter(0))), notifier)
    await prepare_drafts(ctx)
    draft = (await drafts(session))["1"]
    draft.status = DraftStatus.PENDING
    draft.rewrites = 1
    await session.commit()
    # Переписанное письмо: та же копия старого, потом новое; модель снизила оценку
    llm = FakeLLM(answer(letter(0), score=30), answer(letter(1), score=30))
    ctx["drafts"].llm = llm

    stats = await prepare_drafts(ctx)

    assert stats == {"ready": 1}
    draft = (await drafts(session))["1"]
    assert draft.status == DraftStatus.READY
    assert draft.letter == letter(1)
    assert draft.score == 30
    assert len(notifier.sent) == 2


async def test_unsent_card_is_resent_next_run(session: AsyncSession) -> None:
    await prepare(session, "1")
    await session.commit()
    notifier = FakeNotifier([TimeoutError()])
    ctx = drafts_ctx(session, FakeLLM(answer(letter(0))), notifier)

    await prepare_drafts(ctx)
    assert (await drafts(session))["1"].sent_at is None

    await prepare_drafts(ctx)
    assert (await drafts(session))["1"].sent_at is not None
    assert len(notifier.sent) == 1


async def test_worker_noop_cases(session: AsyncSession) -> None:
    await prepare(session, "1")
    await session.commit()
    ctx = drafts_ctx(session, FakeLLM())

    # Другой воркер уже пишет черновики
    ctx["redis"].held.add(DRAFTS_LOCK_KEY)
    assert await prepare_drafts(ctx) == {}
    ctx["redis"].held.clear()
    # Получатель не писал боту /start
    ctx["drafts"].chat_id = 1
    assert await prepare_drafts(ctx) == {}
    # Черновики выключены
    ctx["drafts"] = None
    assert await prepare_drafts(ctx) == {}
    assert await drafts(session) == {}


async def test_repository_counts_filtered_and_ownership(session: AsyncSession) -> None:
    user_id, _ = await prepare(session, "1", "2")
    repo = DraftRepository(session)
    await repo.queue_new_vacancies(user_id, LONG_AGO)
    by_vacancy = await drafts(session)
    by_vacancy["1"].status = DraftStatus.FILTERED
    by_vacancy["1"].filter_reason = "зарплата"
    await session.flush()

    assert await repo.counts(user_id, LONG_AGO) == {
        DraftStatus.FILTERED: 1,
        DraftStatus.PENDING: 1,
    }
    assert [d.vacancy_id for d in await repo.filtered(user_id, LONG_AGO)] == ["1"]
    assert await repo.get_owned(user_id, by_vacancy["1"].id) is not None
    assert await repo.get_owned(user_id + 1, by_vacancy["1"].id) is None
    assert await repo.next_pending(user_id, {by_vacancy["2"].id}) is None


class DraftsScheduler(FakeScheduler):
    def __init__(self) -> None:
        super().__init__()
        self.drafts = 0

    async def schedule_drafts(self) -> None:
        self.drafts += 1


@pytest.fixture(scope="module")
def drafts_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.include_router(drafts_router)
    return dp


class DraftsHarness(BotHarness):
    def __init__(self, dp: Dispatcher, session: AsyncSession) -> None:
        super().__init__(dp, session)
        self.scheduler: DraftsScheduler = DraftsScheduler()
        dp["scheduler"] = self.scheduler


@pytest.fixture
async def harness(
    drafts_dispatcher: Dispatcher, session: AsyncSession
) -> AsyncIterator[DraftsHarness]:
    h = DraftsHarness(drafts_dispatcher, session)
    yield h
    await drafts_dispatcher.storage.close()


CARD_MARKUP = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="↗️ Открыть", url="https://hh.ru/vacancy/1")],
        [InlineKeyboardButton(text="✅ Откликнулся", callback_data="x")],
    ]
)


async def ready_draft(session: AsyncSession) -> ApplicationDraft:
    user_id, _ = await prepare(session, "1")
    await DraftRepository(session).queue_new_vacancies(user_id, LONG_AGO)
    draft = (await drafts(session))["1"]
    draft.status = DraftStatus.READY
    draft.letter = letter(0)
    await session.commit()
    return draft


async def mark_status(session: AsyncSession) -> MarkStatus | None:
    session.expire_all()
    status: MarkStatus | None = await session.scalar(select(VacancyMark.status))
    return status


@pytest.mark.parametrize(
    ("action", "status", "mark", "note"),
    [
        ("applied", DraftStatus.APPLIED, MarkStatus.APPLIED, "✅ Отмечено: откликнулся"),
        ("dismiss", DraftStatus.DISMISSED, MarkStatus.SKIPPED, "✖️ Пропущено"),
    ],
)
async def test_card_buttons_mark_vacancy(
    harness: DraftsHarness, action: str, status: DraftStatus, mark: MarkStatus, note: str
) -> None:
    draft = await ready_draft(harness.session)

    await harness.press(DraftCB(action=action, id=draft.id).pack(), CARD_MARKUP)

    assert (await drafts(harness.session))["1"].status == status
    assert await mark_status(harness.session) == mark
    # Под карточкой осталась только ссылка
    [edit] = harness.api.sent(EditMessageReplyMarkup)
    assert [[b.text for b in row] for row in edit.reply_markup.inline_keyboard] == [["↗️ Открыть"]]
    assert harness.last_text() == note
    assert harness.scheduler.drafts == 0


async def test_rewrite_button_requeues_draft(harness: DraftsHarness) -> None:
    draft = await ready_draft(harness.session)

    await harness.press(DraftCB(action="rewrite", id=draft.id).pack(), CARD_MARKUP)

    draft = (await drafts(harness.session))["1"]
    assert draft.status == DraftStatus.PENDING
    assert draft.rewrites == 1
    assert harness.scheduler.drafts == 1
    assert await mark_status(harness.session) is None


async def test_foreign_draft_is_not_found(harness: DraftsHarness) -> None:
    await harness.press(DraftCB(action="applied", id=999_999).pack(), CARD_MARKUP)

    assert harness.alerts() == ["Черновик не найден"]


async def test_drafts_command(harness: DraftsHarness) -> None:
    await harness.send("/drafts")
    assert harness.last_text().startswith("Черновиков за неделю нет")

    draft = await ready_draft(harness.session)
    draft.status = DraftStatus.FILTERED
    draft.filter_reason = "оценка 20 ниже порога 50"
    await harness.session.commit()
    await harness.send("/drafts")

    text = harness.last_text()
    assert "🙈 Отсеяно моделью: 1" in text
    assert 'href="https://hh.ru/vacancy/1">Вакансия 1</a>: оценка 20 ниже порога 50' in text
    assert len(harness.api.sent(SendMessage)) == 2
