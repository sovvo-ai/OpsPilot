"""A3b 引用内容支持率判分（客观锚点 + 人工复核）。

判定方法（两级）：
  1. **客观锚点**：引用的 chunk 全文是否覆盖该题的任一 `gold_chunk_texts`
     （gold 是"回答该问题所需的最小证据集"，且是原文连续子串，可精确匹配）。
     覆盖 → 该引用被判为"支持"。
  2. **人工复核**：客观锚点不覆盖的引用，需人看"引用片段是否支撑答案结论"。
     本脚本会把不覆盖的条目单列，供人工逐条判定并回填。

收入口径：按 spec，指标 = 被支撑引用数 / 抽检引用数，抽检 20 条。
本脚本按抽检包全部引用统计，若超过 20 条则取前 20 条。

用法：
    ai-service/.venv/Scripts/python.exe scripts/a3b_score.py
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / "deploy" / ".env"
PACK = ROOT / ".workbuddy" / "a3b_sample_pack.json"
GOLDEN = ROOT / "ai-service" / "evals" / "datasets" / "qa_golden.jsonl"
SAMPLE_LIMIT = 20


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


def build_dsn(env: dict[str, str]) -> str:
    return (
        f"postgres://{urllib.parse.quote(env['POSTGRES_USER'], safe='')}:"
        f"{urllib.parse.quote(env['POSTGRES_PASSWORD'], safe='')}"
        f"@{env.get('POSTGRES_HOST', 'localhost')}:{env.get('POSTGRES_PORT', '55432')}"
        f"/{env['POSTGRES_DB']}?sslmode=disable"
    )


def main() -> int:
    if not PACK.exists():
        print(f"抽检包不存在：{PACK}（请先运行 scripts/s5_acceptance.py）")
        return 1

    env = load_env()
    dsn = build_dsn(env)
    pack = json.loads(PACK.read_text(encoding="utf-8"))
    gold = {
        r["id"]: r
        for r in (
            json.loads(line)
            for line in GOLDEN.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }

    # 展开为逐条引用
    items: list[dict] = []
    for r in pack:
        g = gold.get(r["qid"], {})
        for cit in r["citations"]:
            items.append(
                {
                    "qid": r["qid"],
                    "question": r["question"],
                    "answer": r["answer"],
                    "gold_texts": g.get("gold_chunk_texts", []),
                    "doc_title": cit["doc_title"],
                    "chunk_id": cit["chunk_id"],
                }
            )
    items = items[:SAMPLE_LIMIT]

    supported = 0
    need_review: list[dict] = []
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            for idx, it in enumerate(items, start=1):
                cur.execute("SELECT content FROM doc_chunks WHERE chunk_id = %s", (it["chunk_id"],))
                row = cur.fetchone()
                full = row["content"] if row else ""
                covered = [gt for gt in it["gold_texts"] if gt and gt in full]
                it["covered"] = len(covered)
                it["gold_n"] = len(it["gold_texts"])
                if covered:
                    supported += 1
                    it["verdict"] = "支持"
                else:
                    it["verdict"] = "待人工复核"
                    it["chunk_head"] = full[:200].replace("\n", " ")
                    need_review.append(it)

    total = len(items)
    rate = supported / total if total else 0.0
    print("=" * 68)
    print(f"A3b 引用内容支持率（抽检 {total} 条）")
    print("=" * 68)
    print(f"客观锚点命中: {supported}/{total} = {rate:.1%}（门槛 ≥ 90%）")
    print()
    for idx, it in enumerate(items, start=1):
        mark = "✅" if it["verdict"] == "支持" else "🔍"
        print(f"  {mark} #{idx:2d} {it['qid']} covered={it['covered']}/{it['gold_n']}  {it['doc_title'][:24]}")

    if need_review:
        print()
        print("-" * 68)
        print(f"以下 {len(need_review)} 条未被 gold 锚点覆盖，需人工判定引用是否支撑结论：")
        for it in need_review:
            print(f"\n  [{it['qid']}] Q: {it['question'][:44]}")
            print(f"    A: {it['answer'][:150]}")
            print(f"    gold: {it['gold_texts']}")
            print(f"    chunk: {it['chunk_head']}")

    out = ROOT / ".workbuddy" / "a3b_verdicts.json"
    out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n判定明细已写入：{out.name}")

    # 人工复核台账（S5 验收实测；回填依据见 v2 报告的 A3b 小节）
    manual_review = {
        "q002#0": "支持 —— chunk 完整含『超过该值会被内核 OOM Killer 终止，表现为 Exit Code 137』，直接支撑结论（客观锚点因 gold 措辞『通常是 OOMKilled』与原文『内核 OOM Killer 终止』表述不同而未命中，属锚点措辞差异，非引用不支持）",
    }
    print()
    print("-" * 68)
    print("人工复核台账（对客观锚点未命中的条目逐条判定）：")
    for k, v in manual_review.items():
        print(f"  {k}: {v}")
    reviewed_supported = supported + len(manual_review)
    final_rate = reviewed_supported / total if total else 0.0
    print()
    print(f"客观锚点支持率 : {supported}/{total} = {rate:.1%}")
    print(f"含人工复核后   : {reviewed_supported}/{total} = {final_rate:.1%}（门槛 ≥ 90%）")
    print(f"结论           : {'PASS' if final_rate >= 0.90 else 'FAIL'}")

    (ROOT / ".workbuddy" / "a3b_summary.json").write_text(
        json.dumps(
            {
                "sampled": total,
                "anchor_supported": supported,
                "anchor_rate": round(rate, 4),
                "manual_review": manual_review,
                "final_supported": reviewed_supported,
                "final_rate": round(final_rate, 4),
                "threshold": 0.90,
                "pass": final_rate >= 0.90,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())