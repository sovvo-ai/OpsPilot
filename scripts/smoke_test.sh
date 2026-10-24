#!/usr/bin/env bash
# smoke_test.sh —— 一键冒烟：确认服务栈可用且**真实链路通**。
#
# 覆盖：健康检查 → 鉴权边界 → 取 token → 上传种子文档 → 轮询 completed → 提问一次
#
# 用法（项目根目录）：
#     bash scripts/smoke_test.sh
#
# 环境变量：
#     APP_PORT    后端端口（默认 8081）
#     PYTHON      Python 解释器（默认用 ai-service/.venv；Linux/macOS 为 bin/python）
#
# 说明：本脚本用 curl 只打**不含中文**的请求（health / 鉴权边界）。
# 含中文的调用（提问）走 Python httpx —— Windows shell 会破坏中文参数
# （见 docs/testing/test-plan.md G7）。
#
# 历史：S3 版本断言 `POST /documents` 返回 501（当时是占位）；接口实现后
# 未鉴权实为 401，脚本失效。S8 按"陌生人照做不卡"的要求重写为真实链路冒烟。

set -euo pipefail

APP_PORT="${APP_PORT:-8081}"
BASE="http://127.0.0.1:${APP_PORT}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 选 Python：优先项目 venv，其次 PATH 里的 python3/python
if [ -n "${PYTHON:-}" ]; then
    :
elif [ -x "$ROOT/ai-service/.venv/Scripts/python.exe" ]; then
    PYTHON="$ROOT/ai-service/.venv/Scripts/python.exe"
elif [ -x "$ROOT/ai-service/.venv/bin/python" ]; then
    PYTHON="$ROOT/ai-service/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
else
    PYTHON="python"
fi

echo "== 冒烟测试开始：${BASE} =="
echo "   Python: $PYTHON"

fail() { echo "ERROR: $1"; exit 1; }

# --- 1. 健康检查 ---
echo "[live]"
curl -fsS "${BASE}/health/live" | "$PYTHON" -m json.tool

echo "[ready]"
curl -fsS "${BASE}/health/ready" | "$PYTHON" -m json.tool

# --- 2. 鉴权边界：未带 token 访问业务端点应 401 ---
echo -n "[未鉴权 documents -> 401] "
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BASE}/api/v1/documents")
echo "$code"
[ "$code" = "401" ] || fail "期望 401，实际 $code（鉴权异常）"

echo -n "[不存在的路径（未鉴权）-> 401] "
code=$(curl -s -o /dev/null -w "%{http_code}" "${BASE}/api/v1/not-exists")
echo "$code"
# 鉴权中间件在路由匹配**之前**执行，因此未带 token 时连不存在的路径也返回 401
# （这是合理的安全默认：不向未鉴权者泄露"该路径是否存在"）
[ "$code" = "401" ] || fail "期望 401，实际 $code"

# --- 3. 真实链路（含中文，走 Python httpx）：token → 上传 → 轮询 → 提问 ---
echo "[端到端链路]"
"$PYTHON" - "$BASE" "$ROOT" <<'PY'
import sys, time, pathlib, httpx

base, root = sys.argv[1], pathlib.Path(sys.argv[2])

tok = httpx.post(f"{base}/api/v1/auth/dev-token",
                 params={"user": "demo-user"}, timeout=10).json()["token"]
h = {"Authorization": f"Bearer {tok}"}
print("  token ok")

seed = root / "scripts" / "seed_docs"
files = sorted(seed.glob("*.md"))
if not files:
    print(f"  ! 未找到种子文档：{seed}")
    sys.exit(1)

doc_ids = []
for p in files:
    with p.open("rb") as f:
        r = httpx.post(f"{base}/api/v1/documents", headers=h,
                       files={"file": (p.name, f, "text/markdown")}, timeout=30)
    if r.status_code not in (201, 202):
        print(f"  ! 上传 {p.name} 失败：{r.status_code} {r.text[:120]}")
        sys.exit(1)
    doc_ids.append(r.json()["doc_id"])
print(f"  已上传 {len(doc_ids)} 篇")

for did in doc_ids:
    for _ in range(120):
        d = httpx.get(f"{base}/api/v1/documents/{did}", headers=h, timeout=10).json()
        if d.get("ingest_status") in ("completed", "failed"):
            if d.get("ingest_status") != "completed":
                print(f"  ! 摄取失败：{did} {d.get('ingest_status')}")
                sys.exit(1)
            break
        time.sleep(1)
print(f"  {len(doc_ids)} 篇摄取 completed")

sid = httpx.post(f"{base}/api/v1/sessions", headers=h,
                 json={"title": "smoke"}, timeout=10).json()["id"]
resp = httpx.post(f"{base}/api/v1/sessions/{sid}/messages", headers=h,
                  json={"question": "Pod 一直 CrashLoopBackOff 应该怎么排查？"},
                  timeout=180).json()
m = resp.get("message", {})
atype = m.get("answer_type")
cits = len(m.get("citations") or [])
print(f"  提问 -> answer_type={atype}, citations={cits}")
if atype != "answered" or cits == 0:
    print("  ! 期望 answered 且带引用")
    sys.exit(1)
print("  端到端链路 ok")
PY

echo "== 冒烟测试通过 =="