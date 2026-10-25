# Деплой на VPS (Yandex Cloud и др.)

> Бесплатный вариант — домашний компьютер с Windows: [WINDOWS.md](WINDOWS.md).

Одна виртуальная машина, на ней через docker compose работают: PostgreSQL, Redis, миграции,
воркер (поиск вакансий + отправка уведомлений) и Telegram-бот. Входящие порты, кроме SSH, не нужны —
бот получает сообщения через long polling.

Ниже `<IP>` — публичный IP машины, `<user>` — логин, который ты укажешь при её создании.

---

## 1. Создать виртуальную машину

Консоль Yandex Cloud → **Compute Cloud** → **Создать ВМ**:

| Параметр | Значение |
|---|---|
| Образ | Ubuntu 24.04 LTS |
| Зона | любая (`ru-central1-a`, `-b` или `-d`) |
| Диск | HDD, 20 ГБ |
| Платформа | Intel Ice Lake, **2 vCPU, гарантированная доля 20%, 2 ГБ RAM** |
| Прерываемая | **нет** (прерываемую останавливают минимум раз в сутки — уведомления пропадут) |
| Сеть | публичный IP — автоматически (по желанию потом сделать его статическим) |
| Доступ | логин `<user>` + твой публичный SSH-ключ (`cat ~/.ssh/id_ed25519.pub`; если ключа нет — `ssh-keygen -t ed25519`) |

Стоимость посчитай в калькуляторе на сайте Yandex Cloud; при первой регистрации даётся стартовый грант.

**Группа безопасности**: входящий трафик — только TCP 22 (SSH). Исходящий — всё.

## 2. Первая проверка: hh.ru с российского IP

```bash
ssh <user>@<IP>
curl -s -o /dev/null -w "%{http_code}\n" \
  -H "HH-User-Agent: getFirstOffer/0.1 (lesha1bars@gmail.com)" \
  "https://api.hh.ru/vacancies?text=frontend&per_page=1"
```

- `200` — значит, `403` был из-за зарубежного IP (VPN). Дальше всё заработает.
- `403` — hh.ru требует токен приложения: дождись одобрения на dev.hh.ru и заполни `HH_APP_TOKEN` (шаг 4).

## 3. Docker и код

```bash
# Docker (официальный скрипт) и права для текущего пользователя
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
exit   # перезайди по SSH, чтобы группа docker применилась
```

Ключ, которым сервер будет читать репозиторий (только чтение):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N "" -C "getfirstoffer-server"
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
EOF
cat ~/.ssh/github_deploy.pub
```

Публичный ключ добавь в GitHub: репозиторий → **Settings → Deploy keys → Add deploy key**
(галочку «Allow write access» не ставить). Затем:

```bash
git clone git@github.com:AlexeyBarsukov/getFirstOffer.git ~/getFirstOffer
cd ~/getFirstOffer
```

## 4. Настройки (`.env` на сервере)

```bash
cp .env.example .env
chmod 600 .env
# случайные пароль БД и секрет — подставь в .env
openssl rand -hex 24   # → POSTGRES_PASSWORD
openssl rand -hex 32   # → JWT_SECRET
nano .env
```

Что заполнить:

| Переменная | Значение |
|---|---|
| `POSTGRES_PASSWORD` | результат `openssl rand -hex 24` (только буквы и цифры — он вставляется в URL) |
| `HH_USER_AGENT` | `getFirstOffer/0.1 (lesha1bars@gmail.com)` |
| `HH_CLIENT_ID`, `HH_CLIENT_SECRET`, `HH_APP_TOKEN` | когда одобрят заявку на dev.hh.ru: впиши ID и Secret, затем `docker compose -f docker-compose.prod.yml run --rm worker python -m app.sources.hh_token` и вставь токен в `HH_APP_TOKEN` |
| `TELEGRAM_BOT_TOKEN` | токен от @BotFather |
| `ADMIN_CHAT_ID` | `785137155` — сюда придут алерты о проблемах |
| `JWT_SECRET` | результат `openssl rand -hex 32` |

`DATABASE_URL`, `REDIS_URL` и `APP_ENV` на сервере задаёт `docker-compose.prod.yml` — их можно не трогать.

## 5. Запуск

> ⚠️ Перед запуском останови бота на своём компьютере: с одним токеном Telegram отдаёт сообщения
> только одному процессу.

```bash
./deploy/deploy.sh
```

Скрипт подтягивает последний `main`, собирает образ и (пере)запускает сервисы.
Через минуту все сервисы должны быть `running`, воркер — `healthy`, `migrate` — `exited (0)`:

```bash
docker compose -f docker-compose.prod.yml ps -a
```

База на сервере новая — в боте снова нажми `/start` и создай подписки.

### Полезные команды

```bash
cd ~/getFirstOffer
docker compose -f docker-compose.prod.yml logs -f worker      # поиск и отправка
docker compose -f docker-compose.prod.yml logs -f bot
docker compose -f docker-compose.prod.yml logs worker | grep vacancy_found   # задержка latency_s
docker compose -f docker-compose.prod.yml restart worker bot
docker compose -f docker-compose.prod.yml down                # остановить (данные сохранятся)
```

## 6. Мониторинг

Алерты приходят в Telegram на `ADMIN_CHAT_ID`:

- ⚠️ 5 циклов подряд (≈5 минут) не удалось проверить ни одной подписки — с текстом последней ошибки
  (например, `403` от hh.ru). ✅ — когда снова заработало.
- 🛑 воркер не отрабатывал больше 5 минут (упал или завис). ✅ — когда вернулся.

Упавшие контейнеры Docker перезапускает сам (`restart: unless-stopped`), в том числе после перезагрузки ВМ.

## 7. Бэкапы базы

Ежедневный дамп в `~/backups`, хранятся 14 дней:

```bash
crontab -e
# добавить строку:
0 3 * * * $HOME/getFirstOffer/deploy/backup.sh >> $HOME/backup.log 2>&1
```

Проверить вручную: `./deploy/backup.sh`.

**Копия в Object Storage (по желанию, чтобы бэкап пережил потерю ВМ):**

1. Object Storage → создать бакет (приватный), например `getfirstoffer-backups`.
2. Сервисный аккаунт с ролью `storage.uploader` → создать **статический ключ доступа**.
3. На сервере:

```bash
cat > deploy/backup.env <<'EOF'
S3_BUCKET=getfirstoffer-backups
AWS_ACCESS_KEY_ID=<идентификатор ключа>
AWS_SECRET_ACCESS_KEY=<секретный ключ>
EOF
chmod 600 deploy/backup.env
./deploy/backup.sh
```

**Восстановление** из дампа:

```bash
gunzip -c ~/backups/getfirstoffer-YYYYMMDD-HHMMSS.sql.gz | \
  docker compose -f docker-compose.prod.yml exec -T postgres \
  sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

## 8. Автодеплой из GitHub

После каждого пуша в `main` GitHub Actions прогоняет проверки (ruff, mypy, тесты) и, если они
прошли, обновляет сервер по SSH.

1. На своём компьютере — отдельный ключ для GitHub Actions:
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/getfirstoffer_ci -N "" -C "github-actions"
   ssh-copy-id -i ~/.ssh/getfirstoffer_ci.pub <user>@<IP>
   ssh-keyscan -H <IP>     # вывод понадобится ниже
   ```
2. GitHub → репозиторий → **Settings → Secrets and variables → Actions**:
   - **Secrets**: `SSH_HOST` = `<IP>`, `SSH_USER` = `<user>`,
     `SSH_PRIVATE_KEY` = содержимое `~/.ssh/getfirstoffer_ci`,
     `SSH_KNOWN_HOSTS` = вывод `ssh-keyscan -H <IP>`.
   - **Variables**: `DEPLOY_ENABLED` = `true`.

Пока `DEPLOY_ENABLED` не задан, CI только проверяет код.

> `deploy/deploy.sh` делает `git reset --hard origin/main` — не правь код прямо на сервере,
> изменения затрутся при следующем деплое. Исключение — `.env` и `deploy/backup.env` (они в `.gitignore`).
