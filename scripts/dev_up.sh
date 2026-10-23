#!/usr/bin/env bash
# dev_up.sh: 本地开发一键启动 Postgres
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ ! -f deploy/.env ]; then
    echo "WARN: deploy/.env 不存在，使用 deploy/.env.example 模板"
    cp deploy/.env.example deploy/.env
fi

docker compose -f deploy/docker-compose.dev.yml --env-file deploy/.env up -d

echo "等待 Postgres 就绪..."
for i in {1..30}; do
    if docker exec opspilot-postgres-dev pg_isready -U "${POSTGRES_USER:-ops_pilot}" -d "${POSTGRES_DB:-ops_pilot}" >/dev/null 2>&1; then
        echo "Postgres 已就绪（端口 ${POSTGRES_HOST_PORT:-55432}）"
        exit 0
    fi
    sleep 1
done

echo "ERROR: Postgres 未在 30 秒内就绪"
exit 1
