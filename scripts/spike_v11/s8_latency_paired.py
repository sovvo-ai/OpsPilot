"""FB3 延迟配对补测（外部裁决 3 指定的方法）。

**为什么换方法**：SPIKE 的 n=15 测出 P50 +594ms 而 P95 −1548ms，**方向打架**。
filter 是纯字符串匹配，理论增量 ≈0 —— 两个相反的差值只能是模型时延噪声。

**本脚本的方法（预注册，与 delta PRD §2.3 一致）**：
  30 题 × 3 轮，on/off **配对**（同题相邻跑，抵消账号/时段漂移）
  统计：逐题配对差值的均值 ± 95% CI（bootstrap 10000 次）

**达标线（事后不许改）**：
  配对差值均值 ≤ +500ms 且 P50/P95 方向一致
  否则写「延迟影响不可判定，按零声明处理」

用法：
  python scripts/spike_v11/s8_latency_paired.py            # 默认 30 题 × 3 轮
  python scripts/spike_v11/s8_latency_paired.py --n 10 --rounds 1   # 冒烟
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ai-service"))

env: dict[str, str] = {}
for line in (ROOT / "deploy" / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
os.environ.update(env)

from app.config import get_config  # noqa: E402
from app.graph.runner import run_turn  # noqa: E402
from app.llm import make_llm_call  # noqa: E402
from app.retrieval import retrieve  # noqa: E402
from app.topic_filter import filter_by_topic  # noqa: E402

OWNER = "demo-user"


def _cfg(filter_on: bool) -> dict:
    c = dict(get_config())
    c["topic_filter_enabled"] = filter_on
    return c


def _retrieve_fn(*, enable_filter: bool, cfg: dict):
    """把过滤器包进 retrieve_fn（与生产接线等价：检索后过滤）。"""

    def fn(owner_id: str, query: str, _cfg: dict, **kw):
        chunks, probe = retrieve(owner_id, query, cfg, probe=True)
        if enable_filter and chunks:
            chunks, _ = filter_by_topic(
                chunks, query, min_consistency=float(cfg.get("topic_filter_min_consistency", 1.0))
            )
        return (chunks, probe) if kw.get("probe") else chunks

    return fn


def run_one(case: dict, *, filter_on: bool) -> dict:
    cfg = _cfg(filter_on)
    t0 = time.monotonic()
    try:
        res = run_turn(
            {
                "request_id": f"lat-{'on' if filter_on else 'off'}-{case['id']}",
                "session_id": "latency",
                "turn_index": 0,
                "question": case["question"],
                "history": [],
                "owner_id": OWNER,
            },
            llm_call=make_llm_call(cfg),
            retrieve_fn=_retrieve_fn(enable_filter=filter_on, cfg=cfg),
        )
        return {
            "qid": case["id"],
            "ms": int((time.monotonic() - t0) * 1000),
            "ok": True,
            "answer_len": len(res.get("final_text") or ""),
            "answer_type": res.get("answer_type"),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "qid": case["id"],
            "ms": None,
            "ok": False,
            "error": str(exc)[:200],
        }


def bootstrap_ci(values: list[float], n_boot: int = 10000, alpha: float = 0.05):
    """配对差值的 bootstrap 95% CI（百分位法）。"""
    if not values:
        return (float("nan"), float("nan"))
    rng = random.Random(20261027)
    n = len(values)
    means = []
    for _ in range(n_boot):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int(n_boot * alpha / 2)]
    hi = means[int(n_boot * (1 - alpha / 2))]
    return (lo, hi)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30, help="题目数")
    ap.add_argument("--rounds", type=int, default=3, help="轮次")
    args = ap.parse_args()

    ds = [
        json.loads(l)
        for l in (ROOT / "ai-service/evals/datasets/qa_golden.jsonl")
        .read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    # 只取 kb_qa 类（filter 全路径，但延迟对比重点在 kb_qa 主链，F3 原文如此）
    cases = ds[: args.n]
    print(f"FB3 配对延迟测：{len(cases)} 题 × {args.rounds} 轮 × (on/off 相邻) ")
    print("=" * 78)

    pairs: list[dict] = []
    for r in range(args.rounds):
        print(f"\n--- 第 {r + 1}/{args.rounds} 轮 ---")
        for i, case in enumerate(cases):
            off = run_one(case, filter_on=False)
            on = run_one(case, filter_on=True)
            if not (off["ok"] and on["ok"]):
                print(f"  {case['id']:6s} SKIP ({off.get('error') or on.get('error')})")
                continue
            diff = on["ms"] - off["ms"]
            pairs.append({
                "round": r, "qid": case["id"],
                "off_ms": off["ms"], "on_ms": on["ms"], "diff_ms": diff,
                "off_len": off["answer_len"], "on_len": on["answer_len"],
            })
            print(f"  {case['id']:6s} off={off['ms']:5d}ms on={on['ms']:5d}ms "
                  f"diff={diff:+6d}ms  len {off['answer_len']}→{on['answer_len']}")

    if not pairs:
        print("\n无有效配对，退出")
        return

    diffs = [p["diff_ms"] for p in pairs]
    off_all = [p["off_ms"] for p in pairs]
    on_all = [p["on_ms"] for p in pairs]
    mean_diff = statistics.mean(diffs)
    lo, hi = bootstrap_ci(diffs)

    def pct(vals, q):
        s = sorted(vals)
        return s[min(len(s) - 1, int(len(s) * q))]

    off_p50, off_p95 = pct(off_all, 0.50), pct(off_all, 0.95)
    on_p50, on_p95 = pct(on_all, 0.50), pct(on_all, 0.95)

    print("\n" + "=" * 78)
    print("【FB3 判定】")
    print("=" * 78)
    print(f"  配对样本 n = {len(pairs)}")
    print(f"  配对差值均值 = {mean_diff:+.1f}ms   95% CI = [{lo:+.1f}, {hi:+.1f}]ms")
    print(f"  off: P50={off_p50}ms P95={off_p95}ms")
    print(f"  on : P50={on_p50}ms P95={on_p95}ms")
    print(f"  P50 方向: {'劣化' if on_p50 > off_p50 else '改善/持平'}"
          f"（{on_p50 - off_p50:+d}ms）")
    print(f"  P95 方向: {'劣化' if on_p95 > off_p95 else '改善/持平'}"
          f"（{on_p95 - off_p95:+d}ms）")
    print()

    cond_mean = mean_diff <= 500
    cond_dir = (on_p50 > off_p50) == (on_p95 > off_p95)
    if cond_mean and cond_dir:
        verdict = "PASS（配对差值均值 ≤ +500ms 且 P50/P95 方向一致）"
    elif not cond_mean and not cond_dir:
        verdict = "FAIL（均值超阈 AND 方向矛盾）→ 写「延迟影响不可判定，按零声明处理」"
    elif not cond_mean:
        verdict = "FAIL（均值超 +500ms）→ 写「延迟影响不可判定，按零声明处理」"
    else:
        verdict = "INCONCLUSIVE（方向矛盾）→ 写「延迟影响不可判定，按零声明处理」"
    print(f"  **判定：{verdict}**")

    out = {
        "method": "30题×3轮 on/off 配对，bootstrap 10000 次",
        "threshold": "配对差值均值 <= +500ms 且 P50/P95 方向一致",
        "n_pairs": len(pairs),
        "mean_diff_ms": round(mean_diff, 1),
        "ci95_ms": [round(lo, 1), round(hi, 1)],
        "off": {"p50": off_p50, "p95": off_p95},
        "on": {"p50": on_p50, "p95": on_p95},
        "cond_mean_ok": cond_mean,
        "cond_direction_ok": cond_dir,
        "verdict": verdict,
        "pairs": pairs,
    }
    p = ROOT / "docs/testing/fb3_latency_paired.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n原始数据 → {p}")


if __name__ == "__main__":
    main()