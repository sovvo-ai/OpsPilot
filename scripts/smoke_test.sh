#!/usr/bin/env bash
# smoke_test.sh: S3 骨架冒烟测试
set -euo pipefail

APP_PORT="${APP_PORT:-8081}"
BASE="http://127.0.0.1:${APP_PORT}"
PYTHON="${PYTHON:-python}"

echo "== 冒烟测试开始：${BASE} =="

# live
echo "[live]"
curl -fsS "${BASE}/health/live" | "$PYTHON" -m json.tool

# ready
echo "[ready]"
curl -fsS "${BASE}/health/ready" | "$PYTHON" -m json.tool

# deps
echo "[deps]"
curl -fsS "${BASE}/health/deps" | "$PYTHON" -m json.tool

# 业务端点 501 占位（POST /api/v1/documents 已注册）
echo -n "[documents 501] "
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BASE}/api/v1/documents")
echo "$code"
if [ "$code" != "501" ]; then
    echo "ERROR: 期望 501，实际 $code"
    exit 1
fi

# 未鉴权内部端点 401（外部无法直接访问 ai-service，这里测 backend 的 404/401 边界）
echo -n "[not found 404] "
code=$(curl -s -o /dev/null -w "%{http_code}" "${BASE}/api/v1/not-exists")
echo "$code"
if [ "$code" != "404" ]; then
    echo "ERROR: 期望 404，实际 $code"
    exit 1
fi

echo "== 冒烟测试通过 =="
