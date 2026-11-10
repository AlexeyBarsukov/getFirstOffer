import pytest

from app.apply.letter_rules import (
    MAX_LENGTH,
    MIN_LENGTH,
    RequiredPhrase,
    check_letter,
    clean_letter,
    find_required_phrase,
    similarity,
)

# Письмо, которое проходит все правила: на нём проверяем каждое нарушение по отдельности
GOOD_LETTER = (
    "Здравствуйте!\n"
    "Увидел, что вы переписываете личный кабинет на React и TypeScript, это ровно мой стек "
    "с 2021 года. Я запускал продукты с нуля: выбирал стек, настраивал проект и проектировал "
    "frontend-архитектуру и ролевую модель. Делал авторизацию, ключевые экраны и общую "
    "библиотеку компонентов на MUI, писал unit-тесты на Jest и React Testing Library. "
    "Готов обсудить задачи на созвоне, удобнее всего написать мне в Telegram "
    "@Aleksey_Barsukov_development.\n"
    "Алексей Барсуков"
)


def test_good_letter_has_no_problems() -> None:
    assert MIN_LENGTH <= len(GOOD_LETTER) <= MAX_LENGTH
    assert check_letter(GOOD_LETTER) == []


@pytest.mark.parametrize(
    ("letter", "problem"),
    [
        (GOOD_LETTER + " 🚀", "есть смайлики"),
        (GOOD_LETTER + " ✅", "есть смайлики"),
        (GOOD_LETTER.replace(", это ровно", " — это ровно"), "есть длинное тире"),
        (GOOD_LETTER.replace(", это ровно", " – это ровно"), "есть длинное тире"),
        ("Здравствуйте! Пишу по вакансии.", "слишком короткое"),
        (GOOD_LETTER + " Ещё немного текста." * 40, "слишком длинное"),
        (GOOD_LETTER + "\n- React\n- TypeScript", "есть список"),
        (GOOD_LETTER + "\n1. React\n2. TypeScript", "есть список"),
        (
            GOOD_LETTER.replace("Готов обсудить", "С большим энтузиазмом готов обсудить"),
            "шаблонные",
        ),
        (GOOD_LETTER.replace("Делал", "Кроме того, делал"), "шаблонные"),
        (GOOD_LETTER.replace("Алексей Барсуков", "[Ваше имя]"), "заготовки"),
        (GOOD_LETTER.replace("Здравствуйте!", "Добрый день!"), "начинаться со «Здравствуйте»"),
    ],
)
def test_letter_problems(letter: str, problem: str) -> None:
    problems = check_letter(letter)

    assert any(problem in p for p in problems), problems


def test_single_dash_line_is_not_a_list() -> None:
    letter = GOOD_LETTER.replace("Готов обсудить", "\n- готов обсудить")

    assert check_letter(letter) == []


def test_ai_phrases_are_case_insensitive_and_named() -> None:
    letter = GOOD_LETTER.replace("Готов обсудить", "Я ИДЕАЛЬНО ПОДХОЖУ и готов обсудить")

    assert check_letter(letter) == ["шаблонные фразы: «идеально подхожу»"]


def test_required_phrase_missing() -> None:
    problems = check_letter(GOOD_LETTER, required_phrase=RequiredPhrase("банан"))

    assert problems == ["нет слова, которое просит работодатель: «банан»"]


def test_required_phrase_found_ignoring_case() -> None:
    letter = GOOD_LETTER.replace("Готов обсудить", "Банан. Готов обсудить")

    assert check_letter(letter, required_phrase=RequiredPhrase("банан")) == []


def test_required_phrase_at_start_replaces_greeting() -> None:
    phrase = RequiredPhrase("Привет, INTERA", at_start=True)
    letter = GOOD_LETTER.replace("Здравствуйте!", "Привет, INTERA!")

    assert check_letter(letter, required_phrase=phrase) == []
    assert check_letter(GOOD_LETTER, required_phrase=phrase) == [
        "нет слова, которое просит работодатель: «Привет, INTERA»"
    ]


def test_required_phrase_at_start_but_in_the_middle() -> None:
    phrase = RequiredPhrase("банан", at_start=True)
    letter = GOOD_LETTER.replace("Готов обсудить", "Банан. Готов обсудить")

    assert check_letter(letter, required_phrase=phrase) == ["письмо должно начинаться с «банан»"]


INVENTED = (
    "выдуман опыт, которого нет у кандидата: {} "
    "(можно только честно: «с этим не работал, но разберусь»)"
)
KNOWN = "React, TypeScript, Redux Toolkit, React Native, MUI, Jest, React Testing Library"


def test_forbidden_facts() -> None:
    letter = GOOD_LETTER.replace("на MUI", "на Angular")

    assert check_letter(letter, forbidden_facts=["Angular", "Svelte"]) == [
        INVENTED.format("Angular")
    ]


def test_forbidden_java_does_not_match_javascript() -> None:
    letter = GOOD_LETTER.replace("на React", "на JavaScript, React")

    assert check_letter(letter, forbidden_facts=["Java", "C#"]) == []
    assert check_letter(letter + " Писал на Java.", forbidden_facts=["Java"]) == [
        INVENTED.format("Java")
    ]


def test_good_letter_uses_only_known_tech() -> None:
    assert check_letter(GOOD_LETTER, known=KNOWN) == []


@pytest.mark.parametrize(
    ("sentence", "invented"),
    [
        # Настоящая выдумка модели из прогона на вакансии Rambler
        ("Работаю с микрофронтендами и Module Federation.", "Module Federation, микрофронтенды"),
        ("Имею опыт с Vue 3 и Nuxt.", "Nuxt, Vue"),
        ("Я настраивал Docker и CI/CD.", "CI/CD, Docker"),
        ("Мой опыт с Next.js поможет вам.", "Next.js"),
        ("Писал backend на Node.js.", "Node.js"),
        ("В прошлых проектах использовал GraphQL.", "GraphQL"),
    ],
)
def test_unknown_tech_claimed_as_experience(sentence: str, invented: str) -> None:
    letter = GOOD_LETTER.replace("Готов обсудить", sentence + " Готов обсудить")

    assert check_letter(letter, known=KNOWN) == [INVENTED.format(invented)]


@pytest.mark.parametrize(
    "sentence",
    [
        # Честно о том, чего нет
        "С Vue не работал, но на React делал похожее и быстро разберусь.",
        "Next.js пока не использовал, готов быстро освоить.",
        "Docker для меня новый, изучу его в первые недели.",
        # О работодателе, а не о себе
        "Вижу, что у вас Vue 3 и микрофронтенды на Module Federation.",
    ],
)
def test_unknown_tech_allowed_outside_claims(sentence: str) -> None:
    letter = GOOD_LETTER.replace("Готов обсудить", sentence + " Готов обсудить")

    assert check_letter(letter, known=KNOWN) == []


def test_react_native_is_not_react() -> None:
    letter = GOOD_LETTER.replace("Готов обсудить", "Работал с React Native. Готов обсудить")

    assert check_letter(letter, known="React Native, TypeScript, MUI, Jest, Testing Library") == [
        INVENTED.format("React")
    ]


@pytest.mark.parametrize(
    ("phrase", "problem"),
    [
        ("У меня 5 лет коммерческого опыта.", None),
        ("У меня более 4 лет опыта.", None),
        ("Имею опыт от 3 лет на React.", "неверный стаж: 3; у кандидата 5 лет опыта"),
        ("У меня 7+ лет опыта.", "неверный стаж: 7; у кандидата 5 лет опыта"),
        # Не о себе: цифры работодателя не трогаем
        ("Ваш продукт развивается 10 лет.", None),
    ],
)
def test_experience_years(phrase: str, problem: str | None) -> None:
    letter = GOOD_LETTER.replace("Готов обсудить", phrase + " Готов обсудить")

    assert check_letter(letter, years=5) == ([problem] if problem else [])


def test_signature_and_telegram() -> None:
    name = "Алексей Барсуков"
    telegram = "@Aleksey_Barsukov_development"
    assert check_letter(GOOD_LETTER, name=name, telegram=telegram) == []

    unsigned = GOOD_LETTER.rsplit("\n", 1)[0]
    assert check_letter(unsigned, name=name, telegram=telegram) == [
        "последняя строка должна быть подписью: Алексей Барсуков"
    ]
    no_contact = GOOD_LETTER.replace("@Aleksey_Barsukov_development", "мне")
    assert check_letter(no_contact, name=name, telegram=telegram) == [
        "нет контакта в Telegram @Aleksey_Barsukov_development"
    ]


def test_similar_to_recent_letter() -> None:
    almost_same = GOOD_LETTER.replace("личный кабинет", "админку")
    different = "Здравствуйте! " + "Совсем другой текст про поддержку пользователей. " * 10

    assert similarity(GOOD_LETTER, almost_same) > 0.9
    assert check_letter(almost_same, recent=[different, GOOD_LETTER]) == [
        "слишком похоже на одно из прошлых писем"
    ]
    assert check_letter(GOOD_LETTER, recent=[different]) == []


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ('"Здравствуйте!"', "Здравствуйте!"),
        ("опыт — 5 лет", "опыт, 5 лет"),
        ("опыт–5 лет", "опыт, 5 лет"),
        ("React , TypeScript", "React, TypeScript"),
        ("**жирный** текст", "жирный текст"),
        ("и так далее…", "и так далее..."),
        ("слово  ,  , слово", "слово, слово"),
        ("  строка  \n\n\n\n  вторая  ", "строка\n\nвторая"),
    ],
)
def test_clean_letter(raw: str, cleaned: str) -> None:
    assert clean_letter(raw) == cleaned


def test_cleaned_dashes_pass_check() -> None:
    letter = clean_letter(GOOD_LETTER.replace(", это ровно", " — это ровно"))

    assert check_letter(letter) == []


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("В сопроводительном письме укажите слово «банан».", RequiredPhrase("банан")),
        ('Напишите в отклике кодовое слово "frontend2026"', RequiredPhrase("frontend2026")),
        ("Добавьте в письмо фразу “Я читал вакансию”", RequiredPhrase("Я читал вакансию")),
        ("Кодовое слово: „лимон“", RequiredPhrase("лимон")),
        ("Секретное слово — апельсин. Ждём откликов", RequiredPhrase("апельсин")),
        (
            "Начните сопроводительное письмо со слова «Хочу в INTERA»",
            RequiredPhrase("Хочу в INTERA", at_start=True),
        ),
        ("Начните письмо с фразы 'Привет'", RequiredPhrase("Привет", at_start=True)),
    ],
)
def test_find_required_phrase(description: str, expected: RequiredPhrase) -> None:
    assert find_required_phrase(description) == expected


@pytest.mark.parametrize(
    "description",
    [
        "Сопроводительное письмо обязательно.",
        "Укажите в письме ваш опыт работы с React.",
        "Мы используем ключевые слова «agile» в названиях спринтов.",
        "",
    ],
)
def test_no_required_phrase(description: str) -> None:
    assert find_required_phrase(description) is None
