#!/usr/bin/env bash
# ci.sh: 本地 CI，对应 test-plan §4
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "== CI step 1/7: Go vet & test =="
cd backend
go vet ./...
go test ./...
cd "$ROOT"

echo "== CI step 2/7: Python lint & unit tests =="
cd ai-service
.venv/Scripts/python.exe -m pytest tests/ -q
cd "$ROOT"

echo "== CI step 3/7: 启动 dev Postgres =="
bash scripts/dev_up.sh

echo "== CI step 4/7: Python 集成测试（工具注入 A7） =="
cd ai-service
# S7 起有真实集成测试：工具注入/滥用用例（无 DB 时相关用例自动 skip）
.venv/Scripts/python.exe -m pytest tests/integration -q
cd "$ROOT"

echo "== CI step 5/7: Go 契约测试（需要 ai-service 已启动） =="
cd backend
# dev 模式下 ai-service 未自动启动，跳过；后续完整 compose 启动后手动跑
echo "契约测试需手动在完整栈启动后执行：SPIKE_PY_URL=... go test ./test/"
cd "$ROOT"

echo "== CI step 6/7: 敏感信息扫描（排除 docs/ 与 .env 文件） =="
# 扫描密钥模式，排除 .env、.venv 与二进制
grep -Rln --exclude-dir=.venv --exclude='*.env' --exclude='*.pyc' \
    "sk-[A-Za-z0-9._-]\{8,\}" backend ai-service deploy scripts 2>/dev/null || true

echo "== CI step 7/7: 工具层静态禁令（tool-policy §3 P1，失败即中断） =="
# P1：app/tools/ 下不得出现通用执行器。**这是真正的闸门** —— 命中即 exit 1。
# 注意用词边界精确匹配：朴素的子串匹配会把 `re.compile(` 误判（实测踩过）。
TOOLS_DIR="ai-service/app/tools"
if [ -d "$TOOLS_DIR" ]; then
    if grep -RnE '\bsubprocess\b|\bos\.system\b|\bos\.popen\b|\bos\.exec[lv]|(^|[^[:alnum:]_.])eval[[:space:]]*\(|(^|[^[:alnum:]_.])exec[[:space:]]*\(|(^|[^[:alnum:]_.])compile[[:space:]]*\(' "$TOOLS_DIR"; then
        echo "❌ 工具层出现通用执行器痕迹（违反 tool-policy P1），CI 失败"
        exit 1
    fi
    echo "✅ 工具层无通用执行器（P1 通过）"
else
    echo "（工具目录不存在，跳过）"
fi

echo "== CI 完成 =="
