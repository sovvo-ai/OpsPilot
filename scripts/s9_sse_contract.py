"""S9 SSE 契约验收（PRD §8.5 六条）。

四条用例（任务书指定）：
  1. **断流** → trace 标记 `interrupted` 且预算停止消耗（§8.5-S4）
  2. **done 必发**（§8.5-S2）
  3. **citation 不半截**（§8.5-S1：流结束后一次性发）
  4. **S6 回退**：客户端不支持流 → 整包 JSON（同一份内容）

运行（项目根目录，ai-service venv）：
    export POSTGRES_HOST=localhost
    ai-service/.venv/Scripts/python.exe scripts/s9_sse_contract.py

纪律：含中文的请求一律 Python httpx（UTF-8），**禁 Windows shell curl**（test-plan G7）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / "deploy" / ".env"
BACKEND = os.getenv("BACKEND_URL", "http://127.0.0.1:8081")
OWNER = "demo-user"

PASS, FAIL = "✅", "❌"
RESULTS: list[tuple[str, bool, str]] = []


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
    return env


def build_dsn(env: dict[str, str]) -> str:
    q = urllib.parse.quote
    return (
        f"postgres://{q(env.get('POSTGRES_USER','ops_pilot'), safe='')}:"
        f"{q(env.get('POSTGRES_PASSWORD',''), safe='')}"
        f"@{env.get('POSTGRES_HOST','localhost')}:{env.get('POSTGRES_PORT','55432')}"
        f"/{env.get('POSTGRES_DB','ops_pilot')}?sslmode=disable"
    )


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {PASS if ok else FAIL} {name}" + (f" — {detail}" if detail else ""))


def token() -> str:
    return httpx.post(f"{BACKEND}/api/v1/auth/dev-token",
                      params={"user": OWNER}, timeout=10).json()["token"]


def new_session(h: dict) -> str:
    return httpx.post(f"{BACKEND}/api/v1/sessions", headers=h,
                      json={"title": "sse-contract"}, timeout=10).json()["id"]


def read_sse(h: dict, sid: str, question: str,
             abort_after_deltas: int | None = None) -> tuple[list[dict], float | None]:
    """读取 SSE 流；`abort_after_deltas` 非空时在收到 N 个 delta 后**强制 RST 断连**。

    为什么用底层 socket + `SO_LINGER(0)` 而不是 httpx 的 `break`：
    httpx 走连接池，`with` 退出**未必真正关闭 TCP 连接**（可能归还池中），
    服务端因此感知不到断连。`SO_LINGER(0)` + close 会发 **RST**，语义上等同
    "客户端进程消失"，是断连场景的**真实模拟**（实测：只有这种方式能触发
    Go 侧 ctx 取消 → 上游关闭 → ai-service 停止生成）。
    """
    import http.client
    import socket
    import struct
    from urllib.parse import urlparse

    events: list[dict] = []
    first_delta_t: float | None = None
    t0 = time.monotonic()
    n_delta = 0

    u = urlparse(BACKEND)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=180)
    body = json.dumps({"question": question})
    conn.request("POST", f"/api/v1/sessions/{sid}/messages/stream", body=body,
                 headers={**h, "Accept": "text/event-stream",
                          "Content-Type": "application/json"})
    resp = conn.getresponse()
    try:
        # 用 readline 逐行读（**不用 for 迭代**：迭代器会预读缓冲，
        # 读到足够多数据后服务端写入才会进内核缓冲被"吸收"，RST 就来不及生效）
        while True:
            raw = resp.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:].strip())
            ev["_t"] = round(time.monotonic() - t0, 2)
            if ev["type"] == "delta":
                n_delta += 1
                if first_delta_t is None:
                    first_delta_t = ev["_t"]
                events.append(ev)
                if abort_after_deltas is not None and n_delta >= abort_after_deltas:
                    break
            elif ev["type"] == "meta":
                events.append(ev)
                # 断连场景：**收到 meta 就立刻断**（最早时机，最容易让服务端
                # 在"下一次写"时发现 EPIPE —— 这是 TCP 语义下唯一可靠的探测点）
                if abort_after_deltas is not None and abort_after_deltas <= 1:
                    break
            else:
                events.append(ev)
    finally:
        if abort_after_deltas is not None:
            # 强制 RST（SO_LINGER=0 → close 发 RST）
            try:
                sock = conn.sock
                if sock is not None:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                    struct.pack("ii", 1, 0))
                    sock.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return events, first_delta_t


def main() -> int:
    env = load_env()
    os.environ.update({k: v for k, v in env.items() if k not in os.environ})
    dsn = build_dsn(env)
    h = {"Authorization": f"Bearer {token()}"}

    print("=" * 74)
    print("S9 SSE 契约验收（PRD §8.5）")
    print("=" * 74)

    # ---- 用例 2/3：done 必发 + citation 不半截 + 首 token ----
    print("\n--- 用例 2/3：done 必发 & citation 一次性 ---")
    sid = new_session(h)
    evs, first_delta = read_sse(h, sid, "Nginx 的 502 和 504 分别代表什么？")
    types = [e["type"] for e in evs]

    has_done = "done" in types
    record("§8.5-S2 done 必发", has_done,
           f"事件序列={types[:3]}…{types[-2:] if len(types)>3 else types}")

    # citation 只能出现一次，且必须在所有 delta **之后**
    cit_idx = [i for i, t in enumerate(types) if t == "citation"]
    last_delta_idx = max([i for i, t in enumerate(types) if t == "delta"], default=-1)
    cit_ok = len(cit_idx) == 1 and cit_idx[0] > last_delta_idx
    record("§8.5-S1 citation 流结束后一次性发（不半截）", cit_ok,
           f"citation 出现 {len(cit_idx)} 次，位置 {cit_idx}，末 delta 位置 {last_delta_idx}")

    # 流式期间**不得**出现 citation（即 delta 之间无 citation）
    between = [t for t in types[:last_delta_idx + 1] if t == "citation"]
    record("§8.5-S1 delta 之间无 citation 插入", between == [], f"中途 citation={between}")

    record("U9 首个 token ≤2s", (first_delta or 99) <= 2.0, f"首 delta={first_delta}s")

    # done 带完整 result（Go 侧同一落库路径的依据）
    done_ev = next((e for e in evs if e["type"] == "done"), {})
    record("done 携带完整 result（供同一落库路径）", bool(done_ev.get("result")),
           f"result keys={len((done_ev.get('result') or {}).keys())}")

    # 落库校验：同一 run 可通过 GET /runs 补全（§8.5-S5）
    run_id = (done_ev.get("result") or {}).get("run_id") or next(
        (e.get("run_id") for e in evs if e["type"] == "meta"), None)
    if run_id:
        gr = httpx.get(f"{BACKEND}/api/v1/runs/{run_id}", headers=h, timeout=10)
        record("§8.5-S5 同一 run 可通过 GET /runs 补全", gr.status_code == 200,
               f"runs HTTP={gr.status_code} node_path={(gr.json().get('node_path') if gr.status_code==200 else '-')}"
               if gr.status_code != 200 else
               f"runs HTTP=200 node_path={gr.json().get('node_path')}")

    # ---- 用例 4：S6 回退（不支持流 → 整包 JSON）----
    print("\n--- 用例 4：S6 回退（客户端不支持流）---")
    sid2 = new_session(h)
    r = httpx.post(f"{BACKEND}/api/v1/sessions/{sid2}/messages/stream",
                   headers={**h, "Accept": "application/json"},   # 不接受 SSE
                   json={"question": "Nginx 502 是什么？"}, timeout=180)
    fallback_ok = r.status_code == 200 and "text/event-stream" not in r.headers.get("content-type", "")
    body: dict[str, Any] = {}
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        pass
    has_msg = bool(body.get("message"))
    record("§8.5-S6 不支持流 → 整包 JSON（同一份内容）",
           fallback_ok and has_msg,
           f"HTTP={r.status_code} ctype={r.headers.get('content-type','')[:30]} "
           f"有 message={has_msg} answer_type={(body.get('message') or {}).get('answer_type')}")

    # ---- 用例 1：断流 → interrupted trace + 停止烧 token ----
    # 说明：客户端断连在 TCP 层是**概率性**可观测的 —— 定长小响应可能被内核发送缓冲
    # 吸收，服务端需"下一次写失败"才察觉（这是 TCP 语义，不是实现缺陷）。
    # 因此用**裸 socket + SO_LINGER(0) 强制 RST** 并把 RST 时机压到最早（读到 meta 即断），
    # 最多重试 3 轮直到服务端确实观察到断连；每轮的 run_id 独立记录。
    print("\n--- 用例 1：客户端断流 → interrupted + 停止消耗 ---")
    interrupted_ok, detail = False, "未观察到 interrupted"
    for attempt in range(1, 4):
        sid3 = new_session(h)
        evs3, _ = read_sse(h, sid3, "请详细说明 Nginx 502 与 504 的排查步骤与配置项，越详细越好。",
                           abort_after_deltas=1)
        meta_run = next((e.get("run_id") for e in evs3 if e["type"] == "meta"), None)
        deltas_got = sum(1 for e in evs3 if e["type"] == "delta")
        print(f"    第 {attempt} 轮：读到 {deltas_got} 个 delta 后强制 RST（run_id={str(meta_run)[:8]}…）")
        if not meta_run:
            continue

        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT terminated_reason, llm_calls FROM agent_runs WHERE id = %s",
                    (meta_run,),
                )
                row = cur.fetchone()
            if row and row["terminated_reason"] == "interrupted":
                interrupted_ok = True
                detail = (f"第 {attempt} 轮：terminated_reason=interrupted, "
                          f"llm_calls={row['llm_calls']}（run_id={str(meta_run)[:8]}）")
                break
            if row:
                detail = f"第 {attempt} 轮: terminated_reason={row['terminated_reason']}"
            time.sleep(1)
        if interrupted_ok:
            break
    record("§8.5-S4 断流后 trace 标记 interrupted", interrupted_ok, detail)

    # 停止烧 token：中断 run 的 llm_calls 应不高于正常 run
    if interrupted_ok:
        with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT llm_calls FROM agent_runs
                    WHERE terminated_reason = 'interrupted'
                    ORDER BY created_at DESC LIMIT 1"""
            )
            interrupted_calls = (cur.fetchone() or {}).get("llm_calls", 0)
            cur.execute(
                """SELECT llm_calls FROM agent_runs
                    WHERE terminated_reason = 'completed' ORDER BY created_at DESC LIMIT 1"""
            )
            normal_calls = (cur.fetchone() or {}).get("llm_calls", 0)
        # 断连时**不继续生成**：中断轮的模型调用数不应高于正常轮
        record("§8.5-S4 断流后停止消耗（llm_calls 不高于正常轮）",
               interrupted_calls <= normal_calls,
               f"interrupted={interrupted_calls}, 正常={normal_calls}")

    # ---- 汇总 ----
    print("\n" + "=" * 74)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"S9 SSE 契约：{passed}/{len(RESULTS)} 通过")
    for name, ok, detail in RESULTS:
        print(f"  {PASS if ok else FAIL} {name}")

    out = PROJECT_ROOT / ".workbuddy" / "s9_sse_contract.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"passed": passed, "total": len(RESULTS),
         "results": [{"name": n, "ok": o, "detail": d} for n, o, d in RESULTS]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已落盘: {out}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())