#!/usr/bin/env bash
# Обновить сервис на сервере до последней версии из main.
# Запускается вручную на сервере или из GitHub Actions по SSH.
set -euo pipefail
cd "$(dirname "$0")/.."

git fetch --quiet origin main
git reset --hard origin/main
docker compose -f docker-compose.prod.yml up -d --build --remove-orphans
docker image prune -f >/dev/null
docker compose -f docker-compose.prod.yml ps
