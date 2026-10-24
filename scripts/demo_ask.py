"""demo 用 CLI：发一次提问并打印人类可读结果（供 demo 录制调用）。

用法：
    python scripts/demo_ask.py "问题"  [--log "日志内容"]  [--show-run]

输出刻意排成"终端演示友好"的样式（分隔线 + 关键字段），
但**所有内容都来自真实接口响应**，不做任何美化性伪造。

纪律：含中文的请求一律 httpx（UTF-8），禁 Windows shell curl（test-plan G7）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND = os.getenv("BACKEND_URL", "http://127.0.0.1:8081")
OWNER = "demo-user"
LINE = "─" * 74


def get_token() -> str:
    r = httpx.post(f"{BACKEND}/api/v1/auth/dev-token", params={"user": OWNER}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def main() -> int:
    ap = argparse.ArgumentParser()
    # 说明：**含换行的内容不要走命令行参数** —— Windows 下 subprocess/COMSPEC
    # 会把参数截断在第一行（实测：`8080 nginx...\n8080 java...` 只剩前半句）。
    # 故多行内容一律通过环境变量 `DEMO_QUESTION` / `DEMO_LOG` 传入。
    ap.add_argument("question", nargs="?", default=None)
    ap.add_argument("--log", default=None, help="attach_log 内容（建议用 DEMO_LOG 环境变量传多行）")
    ap.add_argument("--show-run", action="store_true", help="打印轨迹关键字段")
    ap.add_argument("--title", default="demo")
    args = ap.parse_args()

    # 环境变量优先（可携带换行）
    question = os.getenv("DEMO_QUESTION") or args.question
    log_text = os.getenv("DEMO_LOG") or args.log
    if not question:
        print("缺少问题（用位置参数或 DEMO_QUESTION 环境变量）", file=sys.stderr)
        return 2

    h = {"Authorization": f"Bearer {get_token()}"}
    sid = httpx.post(f"{BACKEND}/api/v1/sessions", headers=h,
                     json={"title": args.title}, timeout=10).json()["id"]

    body: dict = {"question": question}
    if log_text:
        body["attach_log"] = log_text

    t0 = time.monotonic()
    r = httpx.post(f"{BACKEND}/api/v1/sessions/{sid}/messages", headers=h,
                   json=body, timeout=180)
    r.raise_for_status()
    wall = time.monotonic() - t0
    resp = r.json()
    m = resp.get("message", {})

    print()
    print(f"问题: {question}")
    if log_text:
        print(f"附日志: {log_text.strip().splitlines()[0][:66]}")
    print(LINE)

    at = m.get("answer_type")
    icon = {"answered": "✓", "refusal": "⊘ 拒答", "clarify": "? 需澄清"}.get(at, at)
    print(f"answer_type = {icon}")
    print()

    text = (m.get("content") or "").strip()
    for ln in text.splitlines()[:14]:
        print(f"  {ln}")
    if len(text.splitlines()) > 14:
        print("  …")

    cits = m.get("citations") or []
    if cits:
        print()
        print(f"引用（{len(cits)} 条）:")
        for i, c in enumerate(cits[:5], 1):
            title = c.get("doc_title") or "?"
            score = c.get("score")
            st = c.get("status", "?")
            print(f"  [{i}] {title:34s} score={score} status={st}")

    if args.show_run:
        run = httpx.get(f"{BACKEND}/api/v1/runs/{resp['run_id']}", headers=h, timeout=10).json()
        print()
        print(LINE)
        print(f"route       = {run.get('route')}")
        print(f"node_path   = {run.get('node_path')}")
        ts = run.get("retrieval_top_score")
        print(f"top_score   = {ts}")
        print(f"chunk_ids   = {len(run.get('retrieved_chunk_ids') or [])} 个（完整 Top-K）")
        print(f"rewritten   = {run.get('rewritten_query')}")
        tcs = run.get("tool_call_records") or []
        if tcs:
            print("tool_calls:")
            for t in tcs:
                print(f"  {t['tool_name']:22s} {t['status']:5s} {t['duration_ms']:>4}ms  "
                      f"{t['args_digest'][:22]}")
        else:
            print("tool_calls  = （本轮未调用工具）")

    print()
    print(f"耗时 {wall:.2f}s")
    print(LINE)
    return 0


if __name__ == "__main__":
    sys.exit(main())