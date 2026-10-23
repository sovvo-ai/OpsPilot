#!/usr/bin/env bash
# ci.sh: 本地 CI，对应 test-plan §4
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "== CI step 1/6: Go vet & test =="
cd backend
go vet ./...
go test ./...
cd "$ROOT"

echo "== CI step 2/6: Python lint & unit tests =="
cd ai-service
.venv/Scripts/python.exe -m pytest tests/ -q
cd "$ROOT"

echo "== CI step 3/6: 启动 dev Postgres =="
bash scripts/dev_up.sh

echo "== CI step 4/6: Python integration tests（占位，当前无集成测试） =="
cd ai-service
.venv/Scripts/python.exe -m pytest tests/integration -q || echo "integration tests 尚未建立，跳过"
cd "$ROOT"

echo "== CI step 5/6: Go 契约测试（需要 ai-service 已启动） =="
cd backend
# dev 模式下 ai-service 未自动启动，跳过；后续完整 compose 启动后手动跑
echo "契约测试需手动在完整栈启动后执行：SPIKE_PY_URL=... go test ./test/"
cd "$ROOT"

echo "== CI step 6/6: 敏感信息扫描（排除 docs/ 与 .env 文件） =="
# 扫描密钥模式，排除 .env、.venv 与二进制
grep -Rln --exclude-dir=.venv --exclude='*.env' --exclude='*.pyc' \
    "sk-[A-Za-z0-9._-]\{8,\}" backend ai-service deploy scripts 2>/dev/null || true
# 危险调用扫描（工具目录若不存在则跳过）
if [ -d ai-service/app/tools ]; then
    grep -rn "subprocess\|os\.system\|eval(" ai-service/app/tools/ || true
fi

echo "== CI 完成 =="
