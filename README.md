# getFirstOffer

1) Написать собственный сервис в основном думаю тут будет бэкенд и 2 фронтенда
2) Суть какая: Наша задача уведомить клиента как только в hh.ru появляется нужная ему вакансия исходя из фильтров
3) Возможно, можно использовать апи hh.ru? https://api.hh.ru/openapi/redoc#tag/Poisk-rezyume/operation/search-for-resumes
4) В случае если в документации нет нужной нам ручки, будет парсить страницу и добавлять в базу


Требования:
1) Определить инструменты для бэкенда
2) Для фронтенда использовать собственный интерфейс + уведомлять в ~~Max.ru и~~ Telegram бот (Max исключён)

Минимальное MVP:
1) Сделать бэк с уведомлениями в Telegram (ссылку на бота пришлю в ближайшее время, как зарегистрирую его)

Твоя задача для начала расписать пошаговый план реализации сервиса + уточняющие вопросы как эксперта

---

## Быстрый старт (локальная разработка)

Подробный план и документация: [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).

**Нужно:** Python 3.12+, Docker Desktop.

```bash
# 1. Переменные окружения
cp .env.example .env            # заполни HH_USER_AGENT, токены ботов и т.д.

# 2. PostgreSQL (порт 5433) и Redis (порт 6380)
docker compose up -d --wait
docker compose ps               # оба сервиса должны быть в статусе healthy

# 3. Python-окружение
cd backend
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 4. Git-хуки (ruff, mypy и базовые проверки перед коммитом)
.venv/bin/pre-commit install

# 5. Проверки
.venv/bin/ruff check .
.venv/bin/mypy app tests
.venv/bin/pytest
```

Порты 5433 и 6380 выбраны, чтобы не конфликтовать с локально установленными PostgreSQL и Redis.
Их можно поменять в `.env` (`POSTGRES_PORT`, `REDIS_PORT`), не забыв обновить `DATABASE_URL` и `REDIS_URL`.

Остановить контейнеры: `docker compose down` (данные сохранятся в volume),
удалить вместе с данными: `docker compose down -v`.
