# OpsPilot S3 骨架 Makefile
# 用法：make up / make down / make test / make migrate / make prune / make ci

COMPOSE_FILE := deploy/docker-compose.yml
DEV_COMPOSE_FILE := deploy/docker-compose.dev.yml

BACKEND_DIR := backend
AI_SERVICE_DIR := ai-service

POSTGRES_HOST_PORT ?= 55432
APP_PORT ?= 8081

.PHONY: up down dev-up dev-down test test-go test-py migrate migrate-down prune ci smoke logs

## up: 启动完整服务栈（backend + ai-service + postgres）
up:
	docker compose -f $(COMPOSE_FILE) --env-file deploy/.env up -d --build

## down: 停止并移除完整服务栈
down:
	docker compose -f $(COMPOSE_FILE) --env-file deploy/.env down

## dev-up: 仅启动 Postgres（开发本地跑前后端）
dev-up:
	docker compose -f $(DEV_COMPOSE_FILE) --env-file deploy/.env up -d

## dev-down: 停止开发 Postgres
dev-down:
	docker compose -f $(DEV_COMPOSE_FILE) --env-file deploy/.env down

## logs: 查看后端与 AI 服务日志
logs:
	docker compose -f $(COMPOSE_FILE) --env-file deploy/.env logs -f backend ai-service

## test: 运行前后端单元测试（不依赖 Docker）
test: test-go test-py

## test-go: Go 测试 + vet
test-go:
	cd $(BACKEND_DIR) && go vet ./...
	cd $(BACKEND_DIR) && go test ./...

## test-py: Python 单元测试
test-py:
	cd $(AI_SERVICE_DIR) && .venv/Scripts/python.exe -m pytest tests/ -q

## migrate: 对本地 dev 数据库执行 up 迁移（跨平台，解析 deploy/.env）
migrate:
	python scripts/run_migrate.py up

## migrate-down: 对本地 dev 数据库执行一次 down 迁移（开发/测试用）
migrate-down:
	python scripts/run_migrate.py down

## prune: 清理 Docker builder 缓存（磁盘 31G 纪律）
prune:
	docker builder prune -f

## smoke: 冒烟测试（需要完整服务栈已启动）
smoke:
	bash scripts/smoke_test.sh

## ci: 本地 CI：test-plan §4 六步 + 敏感信息扫描（排除 docs/）
ci:
	bash scripts/ci.sh

## dev: 一键本地开发启动（PG + 迁移 + 提示）
dev: dev-up
	@echo "等待 Postgres 就绪..."
	@sleep 3
	@echo "如需执行迁移，运行：make migrate"
	@echo "后端：cd backend && go run ./cmd/api"
	@echo "AI  ：cd ai-service && .venv/Scripts/python.exe -m uvicorn app.main:app --reload"
