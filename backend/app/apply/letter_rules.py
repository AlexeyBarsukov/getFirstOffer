"""Правила сопроводительного письма, которые проверяет код, а не модель (2part.md, раздел 5).

- find_required_phrase: что работодатель просит написать в письме («укажите слово „банан“»);
- clean_letter: безопасная чистка (длинные тире, двойные пробелы, кавычки-«ёлочки» модели);
- check_letter: список нарушений; пустой список — письмо можно отправлять.
"""

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

MIN_LENGTH = 350
MAX_LENGTH = 1100
# Похожесть на одно из последних писем, выше которой письмо считается шаблонным
MAX_SIMILARITY = 0.6

_EMOJI = re.compile(
    "["
    "\U0001f000-\U0001faff"  # смайлики, символы, пиктограммы
    "\U00002600-\U000027bf"  # разное, dingbats (✅, ✨, ❤)
    "\U0001f1e6-\U0001f1ff"  # флаги
    "⭐⭕‼⁉™ℹ〰〽️‍"
    "]"
)
_LONG_DASH = re.compile(r"\s*[—–]\s*")
_BULLET_LINE = re.compile(r"^\s*([-*•]|\d+[.)])\s+", re.M)

# Фразы, по которым HR сразу узнаёт текст нейросети. Сравнение без учёта регистра
AI_PHRASES = (
    "с большим энтузиазмом",
    "с огромным энтузиазмом",
    "идеально подхожу",
    "идеальным кандидатом",
    "идеальный кандидат",
    "динамичной команде",
    "динамичная команда",
    "внести свой вклад",
    "внести значительный вклад",
    "уверен, что смогу",
    "уверен что смогу",
    "рад возможности",
    "буду рад возможности",
    "с нетерпением жду",
    "не могу не отметить",
    "в современном мире",
    "в условиях быстро",
    "является для меня",
    "позвольте представиться",
    "хотел бы выразить",
    "выразить свою заинтересованность",
    "глубоко впечатлен",
    "глубоко впечатлён",
    "впечатлила ваша миссия",
    "ваша миссия",
    "страсть к",
    "я страстно",
    "кроме того,",
    "более того,",
    "таким образом,",
    "в заключение",
    "уникальная возможность",
    "ценный вклад",
    "синергия",
)
# Незаполненные заготовки модели: [Название компании], {vacancy}, <имя>
_PLACEHOLDER = re.compile(r"[\[\]{}<>]")

# Технологии, которые модель любит приписывать кандидату, взяв их из вакансии.
# Название в письме → шаблон. Опыт с технологией можно заявлять, только если она есть
# в профиле (skills и facts)
TECH_TERMS: dict[str, str] = {
    # Не «js» из «Next.js» / «Node.js»
    "JavaScript": r"javascript|(?<![.\w])js\b",
    "TypeScript": r"typescript|(?<![.\w])ts\b",
    "React": r"\breact(?![\s-]*native)(?:\.?js)?\b",
    "React Native": r"react[\s-]*native",
    "Redux": r"\bredux\b",
    "MobX": r"\bmobx\b",
    "Effector": r"\beffector\b",
    "Zustand": r"\bzustand\b",
    "Vue": r"\bvue(?:\.?js)?\b|\bvuex\b|\bpinia\b",
    "Nuxt": r"\bnuxt",
    "Angular": r"\bangular",
    "Svelte": r"\bsvelte",
    "Next.js": r"\bnext\.?js\b|\bnext\b(?=\s*\d|\s*(?:и|,|\.|$))",
    "Node.js": r"\bnode(?:\.?js)?\b|\bnest\.?js\b|\bexpress\.?js\b",
    "Webpack": r"\bwebpack",
    "Vite": r"\bvite\b",
    "Module Federation": r"module\s+federation",
    "микрофронтенды": r"микрофронт|micro[\s-]*frontend",
    "GraphQL": r"graphql|\bapollo\b",
    "WebSocket": r"websocket|веб[\s-]*сокет",
    "Storybook": r"storybook",
    "Tailwind": r"tailwind",
    "SCSS": r"\bscss\b|\bsass\b|\bless\b",
    "Styled Components": r"styled[\s-]*components",
    "MUI": r"\bmui\b|material[\s-]*ui",
    "Ant Design": r"ant[\s-]*design|\bantd\b",
    "Figma": r"\bfigma\b",
    "Jest": r"\bjest\b",
    "React Testing Library": r"testing[\s-]*library|\brtl\b",
    "Playwright": r"playwright",
    "Cypress": r"cypress",
    "Selenium": r"selenium",
    "Postman": r"postman",
    "Jira": r"\bjira\b",
    "Python": r"python|\bdjango\b|\bfastapi\b|\bflask\b",
    "PHP": r"\bphp\b|laravel|\byii",
    "Java": r"\bjava\b(?!\s*script)",
    "Kotlin": r"\bkotlin",
    "Swift": r"\bswift\b",
    "Flutter": r"flutter|\bdart\b",
    "C#": r"\bc#|\.net\b|\basp\.net",
    "C++": r"\bc\+\+",
    "Go": r"\bgolang\b|\bgo\b(?=\s*(?:и|,|\.|/|$))",
    "1С": r"\b1[сc]\b",
    "Bitrix": r"битрикс|bitrix",
    "SQL": r"\bsql\b|postgres|mysql|clickhouse|oracle",
    "Excel": r"\bexcel\b|google\s*sheets",
    "Power BI": r"power\s*bi",
    "Tableau": r"tableau",
    "Docker": r"docker",
    "Kubernetes": r"kubernetes|\bk8s\b",
    "CI/CD": r"\bci/?cd\b|gitlab\s*ci|github\s*actions|jenkins",
    "Linux": r"\blinux\b|\bbash\b",
    "SAP": r"\bsap\b|\bui5\b|\bfiori\b",
    "LLM": r"\bllm\b|\bgpt\b|langchain|\brag\b|нейросет",
}
_TECH_PATTERNS = {name: re.compile(rx, re.I) for name, rx in TECH_TERMS.items()}
# Предложение о себе: кандидат что-то делал, умеет, знает
_FIRST_PERSON = re.compile(
    r"\b(?:я|мне|мой|моя|моё|мои|моего|моей|моим|опыт\w*|работал\w*|работаю|делал\w*|делаю|"
    r"писал\w*|пишу|использовал\w*|использую|разрабатывал\w*|разрабатываю|владею|знаю|умею|"
    r"имею|настраивал\w*|внедрял\w*|применял\w*|занимал\w*|занимаюсь|участвовал\w*|"
    r"создавал\w*|запускал\w*|проектировал\w*|вёл|вел|автоматизировал\w*|тестировал\w*)\b",
    re.I,
)
# …но честно о том, чего нет: «с Vue не работал, но быстро разберусь»
_LEARNING = re.compile(
    r"\bне\s+(?:работал|сталкивал|использовал|приходилось|доводилось|довелось|было)|"
    r"\bне\s+имею|\bпока\s+не\b|разберусь|изучу|освою|подтяну|"
    r"готов\w*\s+(?:быстро\s+)?(?:разобраться|изучить|освоить|подтянуть|перейти|погрузиться)|"
    r"нов\w*\s+для\s+меня",
    re.I,
)
_SENTENCE_END = re.compile(r"(?<=[.!?\n])\s+")
# «5 лет», «3+ года», «более 4 лет»: двузначные числа, годы вида 2021 не попадают
_YEARS = re.compile(r"(?<![\d.,])(\d{1,2})\s*\+?\s*(?:лет|года|год)\b", re.I)


def tech_in(text: str) -> set[str]:
    """Технологии из TECH_TERMS, которые упоминаются в тексте."""
    return {name for name, pattern in _TECH_PATTERNS.items() if pattern.search(text)}


def _term_pattern(term: str) -> re.Pattern[str]:
    if term in _TECH_PATTERNS:
        return _TECH_PATTERNS[term]
    # Целым словом: «Java» не должна находиться в «JavaScript»
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?![\w#+])", re.I)


def _claim_sentences(text: str) -> list[str]:
    """Предложения, где кандидат говорит о своём опыте.

    Сюда не попадает честное «с Vue не работал, но разберусь» и фразы о работодателе
    без первого лица («у вас Vue 3»).
    """
    return [
        s for s in _SENTENCE_END.split(text) if _FIRST_PERSON.search(s) and not _LEARNING.search(s)
    ]


def claimed_terms(text: str, terms: list[str]) -> list[str]:
    """Какие из terms письмо выдаёт за опыт кандидата."""
    claims = _claim_sentences(text)
    return [t for t in terms if any(_term_pattern(t).search(s) for s in claims)]


def unknown_tech(text: str, known: str) -> list[str]:
    """Технологии, заявленные в письме как опыт, которых нет в known (тексте профиля)."""
    missing = sorted(set(TECH_TERMS) - tech_in(known))
    return claimed_terms(text, missing)


def wrong_years(text: str, years: int) -> list[int]:
    """Стаж в письме, который не совпадает с настоящим (допускаем years - 1: «более 4 лет»)."""
    found = {int(n) for s in _claim_sentences(text) for n in _YEARS.findall(s)}
    return sorted(n for n in found if n not in (years, years - 1))


# «В сопроводительном письме укажите слово „банан“», «начните письмо со слова Привет»,
# «напишите кодовое слово: frontend2026». Кавычки любые, слово — до 40 символов
_QUOTED = r"[«\"'“„‘]([^»\"'”“’]{1,40})[»\"'”“’]"
_REQUIRED_PATTERNS = (
    re.compile(
        r"(?:укажите|напишите|добавьте|вставьте|оставьте|упомяните|используйте)"
        r"[^.\n]{0,60}?(?:слово|фразу|кодовое слово|код|пароль)\s*[:\-—]?\s*" + _QUOTED,
        re.I,
    ),
    # «начните письмо со слова …»: индекс _START_PATTERN
    re.compile(
        r"(?:начните|начинайте)\s+(?:сопроводительное\s+)?(?:письмо|отклик|сообщение)"
        r"\s+(?:со?\s+)?(?:слова|фразы)\s*[:\-—]?\s*" + _QUOTED,
        re.I,
    ),
    re.compile(r"(?:кодовое слово|секретное слово|ключевое слово)\s*[:\-—]?\s*" + _QUOTED, re.I),
    # Без кавычек: «кодовое слово: банан»
    re.compile(r"(?:кодовое|секретное)\s+слово\s*[:\-—]\s*([\w\-]{2,40})", re.I),
)
_START_PATTERN = 1


@dataclass(frozen=True)
class RequiredPhrase:
    text: str
    # Работодатель просит именно начать письмо с этой фразы
    at_start: bool = False


def find_required_phrase(description: str) -> RequiredPhrase | None:
    for index, pattern in enumerate(_REQUIRED_PATTERNS):
        if match := pattern.search(description):
            return RequiredPhrase(match.group(1).strip(), at_start=index == _START_PATTERN)
    return None


def clean_letter(text: str) -> str:
    """Убирает то, что можно исправить без перегенерации."""
    text = text.strip().strip('"').strip()
    # «опыт — 5 лет» → «опыт: 5 лет» читается хуже; запятая подходит почти всегда
    text = _LONG_DASH.sub(", ", text)
    text = text.replace("…", "...").replace("**", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" +([,.!?:;])", r"\1", text)
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.casefold(), b.casefold(), autojunk=False).ratio()


def check_letter(
    text: str,
    *,
    required_phrase: RequiredPhrase | None = None,
    recent: list[str] | None = None,
    forbidden_facts: list[str] | None = None,
    known: str | None = None,
    years: int | None = None,
    name: str | None = None,
    telegram: str | None = None,
) -> list[str]:
    """Нарушения правил.

    forbidden_facts — чего у кандидата точно нет (do_not_claim); known — текст профиля:
    технологии из TECH_TERMS, которых в нём нет, нельзя выдавать за опыт; years — настоящий
    стаж; name и telegram — подпись и контакт, которые должны быть в письме.
    """
    problems: list[str] = []
    lowered = text.casefold()
    if _EMOJI.search(text):
        problems.append("есть смайлики")
    if "—" in text or "–" in text:
        problems.append("есть длинное тире")
    if len(text) < MIN_LENGTH:
        problems.append(f"слишком короткое ({len(text)} символов, нужно от {MIN_LENGTH})")
    if len(text) > MAX_LENGTH:
        problems.append(f"слишком длинное ({len(text)} символов, нужно до {MAX_LENGTH})")
    if len(_BULLET_LINE.findall(text)) >= 2:
        problems.append("есть список: письмо должно быть обычным текстом")
    found = [p for p in AI_PHRASES if p in lowered]
    if found:
        problems.append("шаблонные фразы: " + ", ".join(f"«{p.strip(',')}»" for p in found))
    if _PLACEHOLDER.search(text):
        problems.append("остались заготовки в скобках")
    if required_phrase:
        phrase = required_phrase.text.casefold()
        if phrase not in lowered:
            problems.append(f"нет слова, которое просит работодатель: «{required_phrase.text}»")
        elif required_phrase.at_start and not lowered.startswith(phrase):
            problems.append(f"письмо должно начинаться с «{required_phrase.text}»")
    if not (required_phrase and required_phrase.at_start) and not lowered.startswith(
        "здравствуйте"
    ):
        problems.append("письмо должно начинаться со «Здравствуйте»")
    invented = claimed_terms(text, forbidden_facts or [])
    if known is not None:
        invented += [t for t in unknown_tech(text, known) if t not in invented]
    if invented:
        problems.append(
            "выдуман опыт, которого нет у кандидата: "
            + ", ".join(invented)
            + " (можно только честно: «с этим не работал, но разберусь»)"
        )
    if years is not None and (wrong := wrong_years(text, years)):
        problems.append(
            f"неверный стаж: {', '.join(map(str, wrong))}; у кандидата {years} лет опыта"
        )
    if telegram and telegram.casefold() not in lowered:
        problems.append(f"нет контакта в Telegram {telegram}")
    if name:
        last_line = text.rstrip().rsplit("\n", 1)[-1]
        if name.split()[0].casefold() not in last_line.casefold():
            problems.append(f"последняя строка должна быть подписью: {name}")
    for previous in recent or []:
        if similarity(text, previous) > MAX_SIMILARITY:
            problems.append("слишком похоже на одно из прошлых писем")
            break
    return problems
