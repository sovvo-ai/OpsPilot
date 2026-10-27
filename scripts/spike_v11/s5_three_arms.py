"""V1.1 s5：三臂对照实验 —— critic vs 检索主题过滤器，谁当主药。

**背景**（2026-10-27 外部审查的致命发现）：
critic 的启用范围是 route=troubleshooting，但越界缺陷原型 m001（探针配置）
实测走 route=kb_qa。**治病路与发病路错开。**

三臂定义：
  A = critic off / filter off   （基线，应复现 5/5 越界命中）
  B = critic on                 （只走 troubleshooting 草稿路径 → 对 m001 无效，用于证伪）
  C = filter on                 （全路径生效 → 预期消灭污染 chunk）

测量：overreach 流出数 / Recall@5 / MRR@5 / 延迟。

用法：
  python scripts/spike_v11/s5_three_arms.py --runs 5
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# --- 加载 .env（DASHSCOPE_API_KEY 不在 shell env 里，必须读文件）---
ROOT = Path(__file__).resolve().parents[2]
AI = ROOT / "ai-service"
sys.path.insert(0, str(AI))

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

CFG = get_config()
OWNER = "demo-user"

# m001 型问题（越界原型）：全部走 kb_qa
M001_CASES = [
    {
        "qid": "m001",
        "question": "那它怎么配？",
        "history": [
            {"role": "user", "content": "Pod 一直重启，怀疑是探针配置有问题，怎么查？"},
            {"role": "assistant", "content": "先看 describe 的 Last State 与 Events，确认是 liveness 失败还是应用自身退出。"},
        ],
        "turn_index": 1,
        "gold_doc_ids": ["seed:k8s-pod-troubleshooting"],
    },
    {
        "qid": "m001-ctl",
        "question": "Kubernetes 的 livenessProbe 和 readinessProbe 应该怎么配置？",
        "history": [],
        "turn_index": 0,
        "gold_doc_ids": ["seed:k8s-pod-troubleshooting"],
    },
    {
        "qid": "q001",
        "question": "Pod 一直 CrashLoopBackOff 应该怎么排查？",
        "history": [],
        "turn_index": 0,
        "gold_doc_ids": ["seed:k8s-pod-troubleshooting"],
    },
]

# troubleshoot 路径用例（证明 B 臂只在这一路生效）
TS_CASES = [
    {
        "qid": "t001",
        "question": "容器报 Error: CrashLoopBackOff 是什么原因？",
        "history": [],
        "turn_index": 0,
        "attach_log": "Error: CrashLoopBackOff\n  Warning BackOff restarting failed container",
        "gold_doc_ids": ["seed:k8s-pod-troubleshooting"],
    },
]


def run_once(case: dict, *, arm: str, retrieve_fn=None) -> dict:
    """跑一次，返回 {overreach, citations, chunks, latency_ms, route, answer_len}。"""
    t0 = time.monotonic()
    res = run_turn(
        {
            "request_id": f"spike-{arm}-{case['qid']}",
            "session_id": "spike",
            "turn_index": case["turn_index"],
            "question": case["question"],
            "history": case["history"],
            "attach_log": case.get("attach_log"),
            "owner_id": OWNER,
        },
        llm_call=make_llm_call(CFG),
        retrieve_fn=retrieve_fn,
    )
    ms = int((time.monotonic() - t0) * 1000)

    overreach = res.get("citation_overreach") or []
    # 用 result 里的 citation 与 chunks 复核（result 不含 chunks，用 retrieve 单独取）
    return {
        "qid": case["qid"],
        "arm": arm,
        "route": res.get("route"),
        "answer_type": res.get("answer_type"),
        "overreach": len(overreach),
        "overreach_detail": [
            {"mark": o["mark"], "tokens": o["tokens_not_in_source"]} for o in overreach
        ],
        "citations": len(res.get("citations") or []),
        "answer_len": len(res.get("final_text") or ""),
        "latency_ms": ms,
        "retrieved_chunk_ids": res.get("retrieved_chunk_ids") or [],
        "degraded": res.get("degraded"),
    }


def make_filter_retrieve(enable: bool):
    """构造带/不带主题过滤的 retrieve 函数。"""

    def _fn(owner_id: str, query: str, cfg: dict, **kw) -> list | tuple:
        chunks, probe = retrieve(owner_id, query, cfg, probe=True)
        if enable and chunks:
            chunks, _diag = filter_by_topic(chunks, query)
        return (chunks, probe) if kw.get("probe") else chunks

    return _fn


def metrics_for(case: dict, chunk_ids: list[str]) -> dict:
    """算 Recall@5 / MRR@5（用 gold_doc_ids 的 doc 前缀匹配 chunk_id 不现实，
    改用"检索集的 doc 归属是否覆盖 gold doc"这一可算口径）。"""
    # 说明：chunk_id 是 UUID，无法直接映射到 gold_doc_ids（seed:xxx 是合成前缀）。
    # 这里退化为"是否检索到 k8s 文档的 chunk"——通过查库确认。
    return {"note": "见 recall_by_doc 统计"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()

    arms = {
        "A_critic_off_filter_off": make_filter_retrieve(False),
        "C_filter_on": make_filter_retrieve(True),
    }

    results: dict[str, list[dict]] = {}

    print("=" * 78)
    print(f"三臂对照 · m001 型问题（走 kb_qa）· 每臂 {args.runs} 次")
    print("=" * 78)

    for arm_name, rf in arms.items():
        results[arm_name] = []
        print(f"\n--- 臂 {arm_name} ---")
        for case in M001_CASES:
            for r in range(args.runs):
                try:
                    out = run_once(case, arm=arm_name, retrieve_fn=rf)
                except Exception as exc:  # noqa: BLE001
                    print(f"  {case['qid']} run{r} FAIL: {exc}")
                    continue
                results[arm_name].append(out)
                flag = "越界" if out["overreach"] else "干净"
                print(
                    f"  {case['qid']:10s} run{r}  route={out['route']:14s} "
                    f"overreach={out['overreach']} ({flag})  cites={out['citations']}  "
                    f"{out['latency_ms']}ms"
                )

    # 汇总
    print("\n" + "=" * 78)
    print("【汇总 · m001 型（kb_qa 路径）】")
    print("=" * 78)
    for arm_name, rows in results.items():
        if not rows:
            print(f"  {arm_name}: 无数据")
            continue
        n = len(rows)
        with_ov = sum(1 for x in rows if x["overreach"] > 0)
        total_ov = sum(x["overreach"] for x in rows)
        lats = sorted(x["latency_ms"] for x in rows)
        print(
            f"  {arm_name:26s} run={n:2d}  "
            f"有越界={with_ov}/{n} ({with_ov/n:.0%})  "
            f"越界总数={total_ov:2d}  "
            f"P50={lats[n//2]}ms P95={lats[min(n-1, int(n*0.95))]}ms"
        )

    out_path = ROOT / "docs" / "testing" / "s5_three_arms_raw.json"
    out_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n原始数据 → {out_path}")


if __name__ == "__main__":
    main()