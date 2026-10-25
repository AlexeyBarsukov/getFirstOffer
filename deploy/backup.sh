#!/usr/bin/env bash
# Бэкап PostgreSQL: сжатый дамп в $BACKUP_DIR, старше $KEEP_DAYS дней удаляются.
# Если задан S3_BUCKET — дамп дополнительно копируется в Yandex Object Storage.
# Настройки (необязательно) — в deploy/backup.env, см. docs/DEPLOY.md.
# Cron: 0 3 * * * /home/<user>/getFirstOffer/deploy/backup.sh >> /home/<user>/backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f deploy/backup.env ]]; then
  # shellcheck disable=SC1091
  source deploy/backup.env
fi
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"

mkdir -p "$BACKUP_DIR"
file="$BACKUP_DIR/getfirstoffer-$(date -u +%Y%m%d-%H%M%S).sql.gz"

# Пользователь и база берутся из окружения самого контейнера postgres
docker compose -f docker-compose.prod.yml exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --clean --if-exists' \
  | gzip > "$file"
find "$BACKUP_DIR" -name 'getfirstoffer-*.sql.gz' -mtime +"$KEEP_DAYS" -delete

if [[ -n "${S3_BUCKET:-}" ]]; then
  docker run --rm \
    -v "$BACKUP_DIR:/backups:ro" \
    -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_DEFAULT_REGION=ru-central1 \
    amazon/aws-cli:2.17.0 --endpoint-url=https://storage.yandexcloud.net \
    s3 cp "/backups/$(basename "$file")" "s3://$S3_BUCKET/postgres/" --only-show-errors
fi

echo "$(date -u +%FT%TZ) backup ok: $file ($(du -h "$file" | cut -f1))"
