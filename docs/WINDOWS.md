# Запуск на домашнем компьютере с Windows

Бесплатный вариант: сервис работает в Docker на домашнем компьютере, пока он включён.
Всё запускается одной командой и само поднимается после перезагрузки (после входа в Windows).

Команды ниже выполняются в **PowerShell** (Пуск → «PowerShell»).

---

## 0. Проверка: отдаёт ли hh.ru вакансии с этого компьютера

Если на компьютере есть VPN — выключи его (или добавь `api.hh.ru` в исключения).

```powershell
curl.exe -s https://ipinfo.io/country
curl.exe -s -o NUL -w "%{http_code}`n" -H "HH-User-Agent: getFirstOffer/0.1 (lesha1bars@gmail.com)" "https://api.hh.ru/vacancies?text=frontend&per_page=1"
```

- `200` — вакансии пойдут через официальный API.
- `403` — тоже нормально: сервис сам переключится на RSS-ленту поиска hh.ru (`HH_SOURCE=auto`)
  и вакансии всё равно придут. Когда одобрят заявку на dev.hh.ru — впиши токен (шаг 7), и он вернётся на API.

Проверка нужна просто чтобы знать, какой источник будет работать; на запуск она не влияет.

## 1. WSL2 и Docker Desktop

1. PowerShell **от имени администратора** (правый клик → «Запуск от имени администратора»):
   ```powershell
   wsl --install
   ```
   Перезагрузи компьютер.
2. Скачай и установи **Docker Desktop** с docker.com (при установке оставь «Use WSL 2»).
3. Запусти Docker Desktop → ⚙️ **Settings → General** → включи **«Start Docker Desktop when you sign in to your computer»**.
4. Проверь в обычном PowerShell:
   ```powershell
   docker version
   docker compose version
   ```

## 2. Git и код

1. Установи **Git for Windows** (git-scm.com), настройки по умолчанию.
2. Скачай проект (при первом обращении к GitHub откроется окно входа в аккаунт):
   ```powershell
   cd $HOME
   git clone https://github.com/AlexeyBarsukov/getFirstOffer.git
   cd getFirstOffer
   ```

## 3. Настройки `.env`

`.env` с токенами в git не попадает — перенеси его с Mac (флешка, «Избранное» в Telegram и т.п.;
публично не выкладывай — там токен бота). Положи в корень проекта: `C:\Users\<имя>\getFirstOffer\.env`.

Проверь, что в нём есть:

| Переменная | Значение |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен бота |
| `ADMIN_CHAT_ID` | `785137155` — алерты о проблемах |
| `HH_USER_AGENT` | `getFirstOffer/0.1 (lesha1bars@gmail.com)` |
| `POSTGRES_PASSWORD` | любой пароль из букв и цифр (можно оставить как есть — база доступна только внутри Docker) |

`DATABASE_URL` и `REDIS_URL` подставляет `docker-compose.prod.yml` — их не трогай.

## 4. Запуск

> ⚠️ Бот должен работать только в одном месте: если он запущен на Mac — останови его там.

```powershell
docker compose -f docker-compose.prod.yml up -d --build
```

Первая сборка — несколько минут. Затем проверь:

```powershell
docker compose -f docker-compose.prod.yml ps -a
```

`postgres`, `redis`, `worker`, `bot` — `running` (worker — `healthy` через минуту), `migrate` — `exited (0)`.

База новая — в Telegram нажми `/start` в @getFirstOffer_bot и создай подписки.

## 5. Чтобы компьютер не мешал

- **Сон**: Параметры → Система → Питание → «Переводить в спящий режим» → **Никогда**
  (экран можно выключать — это не мешает).
- **Перезагрузки** (например, обновления Windows): контейнеры поднимутся сами, но только после
  входа в Windows — Docker Desktop стартует при входе пользователя. Если хочешь без участия —
  включи автоматический вход в Windows.
- Если компьютер выключался — пропущенные за это время вакансии придут после включения
  (сервис догоняет окно поиска), но уже не «первыми».

## 6. Повседневные команды

```powershell
cd $HOME\getFirstOffer
docker compose -f docker-compose.prod.yml logs -f worker     # поиск и отправка (Ctrl+C — выйти)
docker compose -f docker-compose.prod.yml logs -f bot
docker compose -f docker-compose.prod.yml restart worker bot

# обновиться до последней версии из GitHub
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

Если что-то сломается, придёт алерт в Telegram (воркер завис / hh.ru отвечает ошибками).

## 7. Когда одобрят приложение на dev.hh.ru

1. Впиши в `.env` `HH_CLIENT_ID` и `HH_CLIENT_SECRET`.
2. Получи токен:
   ```powershell
   docker compose -f docker-compose.prod.yml run --rm worker python -m app.sources.hh_token
   ```
3. Вставь выведенный `HH_APP_TOKEN=...` в `.env` и перезапусти:
   ```powershell
   docker compose -f docker-compose.prod.yml up -d
   ```

## 8. Когда работа найдена 🎉

```powershell
docker compose -f docker-compose.prod.yml down -v   # остановить и удалить данные
```

После этого Docker Desktop можно удалить.
