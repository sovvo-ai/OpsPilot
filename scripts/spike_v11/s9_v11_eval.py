"""V1.1 评测：主集 + 诱饵集 on/off 全量对照（FB1–FB4 判定）。

**这是本迭代的验收脚本，不是形式。**

判定项（见 delta PRD §2.1）：
  FB1  诱饵集越界流出 ≤ 基线 10%（21→≤2），**3 轮取均值**
  FB2  主集 Recall@5 on/off 差 = 0 且 MRR ≥ 基线（0.9259 / 0.7728）
  FB3  延迟按配对法（本脚本不测，见 s8_latency_paired.py）
  FB4  哨兵测试存在且绿（本脚本检查文件存在性；实跑由 pytest 负责）

**诱饵集**：`ai-service/evals/datasets/critic_golden.jsonl`
（注意：文件名沿用 SPIKE 时期，内容是"越界诱饵题"，不是 critic 专用）
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
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

from app.chat import detect_citation_overreach  # noqa: E402
from app.config import get_config  # noqa: E402
from app.graph.runner import run_turn  # noqa: E402
from app.llm import make_llm_call  # noqa: E402
from app.retrieval import retrieve  # noqa: E402
from app.topic_filter import filter_by_topic  # noqa: E402

OWNER = "demo-user"
MAIN_DS = ROOT / "ai-service/evals/datasets/qa_golden.jsonl"
BAIT_DS = ROOT / "ai-service/evals/datasets/critic_golden.jsonl"


def _cfg(filter_on: bool) -> dict:
    c = dict(get_config())
    c["topic_filter_enabled"] = filter_on
    return c


def _retrieve_fn(cfg: dict):
    """⚠️ **只做检索，不做过滤** —— 过滤由 retrieval 节点内根据 cfg 决定。

    早期版本在这里也包了一层 filter_by_topic，导致：
      ① 过滤真的生效了（所以越界确实降了）
      ② 但节点内的过滤**没跑**（因为 run_turn 用的是它自己的 get_config()），
         于是 `topic_filter` 审计字段是 None，剔除明细丢失
      ③ 更糟：评测的"on/off"实际是两个不同层级的开关，**测的不是同一件事**

    现在改为：这里只检索，过滤完全交给节点（与生产接线一致）。
    """

    def fn(owner_id: str, query: str, _c: dict, **kw):
        chunks, probe = retrieve(owner_id, query, cfg, probe=True)
        return (chunks, probe) if kw.get("probe") else chunks

    return fn


def _set_filter_config(filter_on: bool) -> None:
    """通过**环境变量 + 清 lru_cache** 切换过滤开关。

    **为什么必须这么做**：`run_turn` 内部调用 `get_config()`，
    不接收外部传入的 cfg。`get_config` 带 `@lru_cache`，
    所以既不能靠传参、也不能靠改 dict —— 必须改环境变量并清缓存。
    （这是契约层面的既有限制，非本次引入。）
    """
    import app.config as cfg_mod

    os.environ["TOPIC_FILTER_ENABLED"] = "true" if filter_on else "false"
    cfg_mod.get_config.cache_clear()


# --- 检索指标（与 run_eval 同口径：gold 子串匹配）---

def _hit_index(chunks: list[dict], gold_texts: list[str]) -> int:
    for i, c in enumerate(chunks):
        body = c["content"]
        for g in gold_texts:
            if g[:20] in body or body[:40] in g:
                return i
    return -1


def eval_recall(cases: list[dict], *, filter_on: bool) -> dict:
    """算 Recall@5 / MRR@5（只跑检索，不跑生成 —— 快且不受生成随机性影响）。"""
    cfg = _cfg(filter_on)
    n = 0
    hits = 0
    mrr = 0.0
    per_case = []
    for d in cases:
        gold = d.get("gold_chunk_texts") or []
        if not gold:
            continue
        try:
            chunks = retrieve(OWNER, d["question"], cfg, probe=False)
            if filter_on and chunks:
                chunks, _ = filter_by_topic(
                    chunks, d["question"],
                    min_consistency=float(cfg.get("topic_filter_min_consistency", 1.0)),
                )
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {d['id']} 检索失败: {exc}")
            continue
        idx = _hit_index(chunks, gold)
        n += 1
        if idx >= 0:
            hits += 1
            mrr += 1.0 / (idx + 1)
        per_case.append({
            "id": d["id"], "hit": idx >= 0, "rank": idx + 1 if idx >= 0 else None,
            "n_chunks": len(chunks),
        })
    return {
        "n": n,
        "recall": hits / n if n else 0.0,
        "mrr": mrr / n if n else 0.0,
        "per_case": per_case,
    }


def eval_overreach(cases: list[dict], *, filter_on: bool) -> dict:
    """跑完整 turn，统计 overreach 流出。

    **filter 开关通过环境变量切换**（见 `_set_filter_config` 的说明）——
    必须让 `retrieval` 节点内部的过滤真正执行，这样 `topic_filter` 审计字段
    才会带回剔除明细。早期版本靠外层 retrieve_fn 包过滤，导致：
    越界降了但审计字段是 None，**测到的不是节点内那条路径**。
    """
    _set_filter_config(filter_on)
    cfg = get_config()
    assert bool(cfg.get("topic_filter_enabled")) is filter_on, (
        f"开关未生效：期望 filter_on={filter_on}，"
        f"实际 topic_filter_enabled={cfg.get('topic_filter_enabled')}"
    )
    rows = []
    for i, case in enumerate(cases, 1):
        try:
            res = run_turn(
                {
                    "request_id": f"eval-{'on' if filter_on else 'off'}-{case['id']}",
                    "session_id": "eval",
                    "turn_index": int(case.get("turn_index", 0)),
                    "question": case["question"],
                    "history": case.get("history") or [],
                    "attach_log": case.get("attach_log"),
                    "owner_id": OWNER,
                },
                llm_call=make_llm_call(cfg),
                retrieve_fn=_retrieve_fn(cfg),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {case['id']} 失败: {exc}")
            continue
        ov = res.get("citation_overreach") or []
        tf = res.get("topic_filter") or {}
        rows.append({
            "id": case["id"],
            "route": res.get("route"),
            "overreach": len(ov),
            "overreach_tokens": [
                t for o in ov for t in o.get("tokens_not_in_source", [])
            ],
            "citations": len(res.get("citations") or []),
            "retrieved": res.get("retrieved_count"),
            "filtered_out": len(tf.get("removed") or []),
            "filter_reason": tf.get("reason"),
            "filter_anchors": tf.get("anchors"),
        })
        print(
            f"  [{i}/{len(cases)}] {case['id']:10s} route={str(res.get('route')):14s} "
            f"overreach={len(ov)} cites={len(res.get('citations') or [])} "
            f"kept={res.get('retrieved_count')} "
            f"filtered={len(tf.get('removed') or [])}"
        )
    total = sum(r["overreach"] for r in rows)
    with_ov = sum(1 for r in rows if r["overreach"] > 0)
    return {
        "n_runs": len(rows),
        "total_overreach": total,
        "runs_with_overreach": with_ov,
        "rows": rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3, help="诱饵集重跑轮数（FB1 要求 3 轮均值）")
    ap.add_argument("--bait-only", action="store_true")
    args = ap.parse_args()

    main_cases = [
        json.loads(line) for line in MAIN_DS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    report: dict = {"commit": _git_commit(), "rounds": args.rounds}

    # ---------- FB2 主集检索指标 ----------
    if not args.bait_only:
        print("=" * 78)
        print("【FB2】主集 35 题 Recall@5 / MRR@5（on/off 同次对照）")
        print("=" * 78)
        off = eval_recall(main_cases, filter_on=False)
        on = eval_recall(main_cases, filter_on=True)
        print(f"  off: n={off['n']} Recall@5={off['recall']:.4f} MRR@5={off['mrr']:.4f}")
        print(f"  on : n={on['n']} Recall@5={on['recall']:.4f} MRR@5={on['mrr']:.4f}")
        d_recall = on["recall"] - off["recall"]
        print(f"  Δ Recall = {d_recall:+.4f}   Δ MRR = {on['mrr'] - off['mrr']:+.4f}")

        # 逐题对比：谁掉了
        off_map = {r["id"]: r for r in off["per_case"]}
        lost = [
            r["id"] for r in on["per_case"]
            if off_map.get(r["id"], {}).get("hit") and not r["hit"]
        ]
        gained = [
            r["id"] for r in on["per_case"]
            if not off_map.get(r["id"], {}).get("hit") and r["hit"]
        ]
        print(f"  丢失命中: {lost or '无'}")
        print(f"  新增命中: {gained or '无'}")

        fb2_pass = abs(d_recall) < 1e-9 and on["mrr"] >= off["mrr"]
        print(f"  **FB2 判定：{'PASS' if fb2_pass else 'FAIL'}**"
              f"（要求 ΔRecall=0 且 MRR≥基线）")
        report["fb2"] = {
            "off": {"recall": round(off["recall"], 4), "mrr": round(off["mrr"], 4)},
            "on": {"recall": round(on["recall"], 4), "mrr": round(on["mrr"], 4)},
            "delta_recall": round(d_recall, 4),
            "delta_mrr": round(on["mrr"] - off["mrr"], 4),
            "lost_hits": lost,
            "gained_hits": gained,
            "pass": fb2_pass,
            "per_case_on": on["per_case"],
        }

    # ---------- FB1 诱饵集越界 ----------
    if BAIT_DS.exists():
        bait = [
            json.loads(line) for line in BAIT_DS.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        print("\n" + "=" * 78)
        print(f"【FB1】诱饵集 {len(bait)} 题 × {args.rounds} 轮（on/off 各跑）")
        print("=" * 78)
        base_rounds, filt_rounds = [], []
        for r in range(args.rounds):
            print(f"\n-- 第 {r + 1} 轮 · off（基线）--")
            base_rounds.append(eval_overreach(bait, filter_on=False))
            print(f"\n-- 第 {r + 1} 轮 · on（filter）--")
            filt_rounds.append(eval_overreach(bait, filter_on=True))

        base_totals = [x["total_overreach"] for x in base_rounds]
        filt_totals = [x["total_overreach"] for x in filt_rounds]
        base_mean = statistics.mean(base_totals)
        filt_mean = statistics.mean(filt_totals)
        print("\n" + "=" * 78)
        print("【FB1 汇总】")
        print("=" * 78)
        print(f"  基线各轮: {base_totals}  均值 = {base_mean:.1f}")
        print(f"  filter各轮: {filt_totals}  均值 = {filt_mean:.1f}")
        target = base_mean * 0.10
        fb1_pass = filt_mean <= target
        print(f"  目标 ≤ 基线 10% = {target:.1f} 处")
        print(f"  **FB1 判定：{'PASS' if fb1_pass else 'FAIL'}（均值 {filt_mean:.1f} vs 目标 {target:.1f}）**")
        report["fb1"] = {
            "bait_n": len(bait),
            "base_rounds": base_totals,
            "filter_rounds": filt_totals,
            "base_mean": round(base_mean, 2),
            "filter_mean": round(filt_mean, 2),
            "target": round(target, 2),
            "pass": fb1_pass,
            "detail": {"base": base_rounds, "filter": filt_rounds},
        }
    else:
        print(f"\n⚠️ 缺诱饵集 {BAIT_DS}，跳过 FB1")

    # ---------- FB4 哨兵存在性 ----------
    sentinels = [
        ROOT / "ai-service/tests/unit/test_topic_filter.py",
        ROOT / "ai-service/tests/unit/test_boundary_sentinels.py",
    ]
    fb4_pass = all(p.exists() for p in sentinels)
    print("\n" + "=" * 78)
    print("【FB4】误杀回归哨兵")
    print("=" * 78)
    for p in sentinels:
        print(f"  {'✅' if p.exists() else '❌'} {p.relative_to(ROOT)}")
    print(f"  **FB4 判定：{'PASS（文件齐备，实跑见 pytest）' if fb4_pass else 'FAIL'}**")
    report["fb4"] = {"files": [str(p.relative_to(ROOT)) for p in sentinels],
                     "all_exist": fb4_pass}

    out = ROOT / "docs/testing/v11_eval_raw.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n原始数据 → {out}")


def _git_commit() -> str:
    import subprocess

    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
        ).strip()[:12]
    except Exception:  # noqa: BLE001
        return "unknown"


if __name__ == "__main__":
    main()