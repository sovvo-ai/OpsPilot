"""kb_qa 10 题延迟复测（方案 A 前后对比，可复算）。

**为什么单独一个脚本**：35 题全量评测跑一次 ~4 分钟，而 P95 的争议点只在 kb_qa
这一组（10 题）。单跑这 10 题能快速做"改前 / 改后"对比，且输出**逐题延迟 + 答案长度**
（答案长度是"简洁度纪律"是否生效的直接证据）。

用法：
    ai-service/.venv/Scripts/python.exe scripts/kb_qa_latency.py --label 基线
产出：
    终端输出 + `.workbuddy/kb_qa_latency_<label>.json`
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / "deploy" / ".env"
DATASET = PROJECT_ROOT / "ai-service" / "evals" / "datasets" / "qa_golden.jsonl"
BASE = os.getenv("BACKEND_URL", "http://127.0.0.1:8081")
USER = "demo-user"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
    return env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run", help="本轮标签（用于输出文件名与对比表）")
    ap.add_argument("--repeat", type=int, default=1, help="每题重复次数（取中位数更稳）")
    args = ap.parse_args()

    env = load_env()
    os.environ.update({k: v for k, v in env.items() if k not in os.environ})

    rows = [json.loads(l) for l in DATASET.read_text(encoding="utf-8").splitlines() if l.strip()]
    kb = [r for r in rows if r.get("category") == "kb_qa"]

    tok = httpx.post(f"{BASE}/api/v1/auth/dev-token", params={"user": USER},
                     timeout=10).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}

    print("=" * 78)
    print(f"kb_qa 延迟复测 —— 标签：{args.label}（{len(kb)} 题 × {args.repeat} 次）")
    print("=" * 78)

    results: list[dict] = []
    for r in kb:
        lats: list[int] = []
        lens: list[int] = []
        for _ in range(args.repeat):
            sid = httpx.post(f"{BASE}/api/v1/sessions", headers=h,
                             json={"title": f"lat-{r['id']}"}, timeout=10).json()["id"]
            t0 = time.monotonic()
            resp = httpx.post(f"{BASE}/api/v1/sessions/{sid}/messages", headers=h,
                              json={"question": r["question"]}, timeout=180).json()
            lats.append(int((time.monotonic() - t0) * 1000))
            lens.append(len(resp["message"].get("content") or ""))
        lat = int(statistics.median(lats))
        ln = int(statistics.median(lens))
        results.append({"qid": r["id"], "question": r["question"],
                        "latency_ms": lat, "latencies": lats, "answer_len": ln})
        print(f"  {r['id']}  lat={lat:>6}ms  len={ln:>4}字  {r['question'][:38]}")

    xs = sorted(x["latency_ms"] for x in results)
    p50 = int(statistics.median(xs))
    p95 = xs[max(0, int(round(0.95 * (len(xs) - 1))))]
    lens = [x["answer_len"] for x in results]

    print("-" * 78)
    print(f"  kb_qa P50 = {p50}ms    P95 = {p95}ms   阈值 8000ms  "
          f"{'✅ 达标' if p95 <= 8000 else '❌ 未达标'}")
    print(f"  答案长度：中位数 {int(statistics.median(lens))} 字，"
          f"最长 {max(lens)} 字，合计 {sum(lens)} 字")
    print(f"  最慢题：{[x['qid'] for x in sorted(results, key=lambda y: -y['latency_ms'])[:3]]}")

    out = PROJECT_ROOT / ".workbuddy" / f"kb_qa_latency_{args.label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "label": args.label, "at": datetime.now(timezone.utc).isoformat(),
        "repeat": args.repeat, "p50_ms": p50, "p95_ms": p95,
        "threshold_ms": 8000, "pass": p95 <= 8000,
        "answer_len_median": int(statistics.median(lens)),
        "answer_len_max": max(lens),
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已落盘: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())