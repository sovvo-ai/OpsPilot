"""回填 A3b 人工复核判定 → 出评测报告 `<version>`（补齐两个 NOT RUN）。

输入：
  - 评测报告 JSON（`--report`，如 `evals/reports/2026-10-24T0115.json`）
  - 复核清单 Markdown（`--review`，`docs/testing/a3b_user_review.md`）

产出：
  - `<report>_<version>.json` / `<report>_<version>.md`（`--version` 指定，默认 `v2`）

回填内容（对齐 `rag-eval-spec.md` §4.1 与清单尾部的 5 步）：
  1. `a3b_reviews` 里 `user_recheck=true` 的条目：`judge="user"`、
     `judged_at`、`verdict`（supported/unsupported）、`provenance`（追溯链路）；
  2. `metrics.citation_support_rate` 与 `metrics.task_success_rate` 用**混合口径**补齐；
  3. `metrics_by_judge` 三口径支持率（用户 / AI / 混合）；
  4. `calibration_log` 追加一条。

**判据说明**：任务成功率按 `verdict` 二值计入（支撑 + 引用有效 + 无幻觉）。
本轮 m001 的"不支撑"性质是**引用越界**（模型把通用知识挂在 KB 引用号下），
按失败项如实计入 —— 不因"看起来答得不错"而豁免。

**残留节**（`--residual`）：把复核清单里"必须同时写进报告"的残留事实原样带进报告，
避免"5/5 支撑"被读成"已根治"。

用法：
    ai-service/.venv/Scripts/python.exe scripts/apply_user_review.py \
        --report ai-service/evals/reports/2026-10-24T1405.json \
        --review docs/testing/a3b_user_review_v3.md \
        --version v3-final --residual docs/testing/v3_residual_notes.md
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

# 清单里每条的小节标题形如： `## 1. \`q028\` · citation_risk`
SECTION_RE = re.compile(r"^##\s+\d+\.\s+`(?P<qid>[^`]+)`", re.MULTILINE)
# 判定行： `判定：[x] 支撑   [ ] 不支撑`
SUPPORT_RE = re.compile(r"判定：\[(?P<sup>[ xX])\]\s*支撑\s+\[(?P<unsup>[ xX])\]\s*不支撑")
PROVENANCE_RE = re.compile(r"\*\*判定归属声明\*\*：(.+?)(?:\n\n|\Z)", re.DOTALL)


def parse_review(path: Path) -> tuple[dict[str, str], str]:
    """解析复核清单 → {qid: verdict} 与 provenance 文本。"""
    text = path.read_text(encoding="utf-8")

    # provenance（头部声明）—— 去掉 Markdown 引用符号与多余空白
    m = PROVENANCE_RE.search(text)
    raw = m.group(1) if m else ""
    provenance = " ".join(
        ln.strip().lstrip(">").strip() for ln in raw.splitlines()
    ).strip()
    provenance = re.sub(r"\s+", " ", provenance)
    # 去掉行内 Markdown 标记（**加粗**、`代码`），保留纯文本；
    # 再清掉跨行加粗未闭合残留的孤立 `**`
    provenance = re.sub(r"\*\*(.+?)\*\*", r"\1", provenance)
    provenance = provenance.replace("`", "").replace("**", "")

    # 逐条解析
    verdicts: dict[str, str] = {}
    heads = list(SECTION_RE.finditer(text))
    for i, h in enumerate(heads):
        qid = h.group("qid")
        seg_end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        seg = text[h.end():seg_end]
        mm = SUPPORT_RE.search(seg)
        if not mm:
            continue
        sup = mm.group("sup").strip().lower() == "x"
        unsup = mm.group("unsup").strip().lower() == "x"
        if sup and not unsup:
            verdicts[qid] = "supported"
        elif unsup and not sup:
            verdicts[qid] = "unsupported"
        elif sup and unsup:
            raise ValueError(f"{qid}: 同时勾选了支撑与不支撑，无法判定")
        else:
            raise ValueError(f"{qid}: 判定行未勾选（既非支撑也非不支撑）")
    return verdicts, provenance


def rate(items: list[dict]) -> float | str:
    judged = [i for i in items if i.get("verdict")]
    if not judged:
        return "NOT RUN"
    return sum(1 for i in judged if i["verdict"] == "supported") / len(judged)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True)
    ap.add_argument("--review", required=True)
    ap.add_argument("--judged-at", default=str(date.today()))
    ap.add_argument("--version", default="v2", help="报告版本号，用于输出文件名与标题")
    ap.add_argument("--residual", default=None,
                    help="残留事实 Markdown（原样写进报告的『残留与已知限制』节）")
    ap.add_argument("--calibration-finding", default=None,
                    help="本轮人工复核的实际发现（默认按 unsupported 自动生成）")
    ap.add_argument("--calibration-resolution", default=None,
                    help="本轮处置说明")
    args = ap.parse_args()

    rep_path = Path(args.report)
    rep = json.loads(rep_path.read_text(encoding="utf-8"))
    verdicts, provenance = parse_review(Path(args.review))

    print(f"解析到 {len(verdicts)} 条判定：{verdicts}")
    if not provenance:
        print("⚠️ 未解析到判定归属声明（provenance 将为空）")

    # --- 1. 回填 a3b_reviews ---
    filled, missing = 0, []
    for a in rep["a3b_reviews"]:
        if not a.get("user_recheck"):
            continue
        v = verdicts.get(a["qid"])
        if not v:
            missing.append(a["qid"])
            continue
        a["judge"] = "user"
        a["judged_at"] = args.judged_at
        a["verdict"] = v
        a["provenance"] = provenance or "外部审查代判+用户追认"
        filled += 1
    if missing:
        print(f"⚠️ 清单里没找到这些条目的判定：{missing}")
    print(f"回填 {filled} 条（判据：judge=user, judged_at={args.judged_at}）")

    # --- 2/3. 重算三口径 ---
    user_items = [a for a in rep["a3b_reviews"] if a.get("user_recheck")]
    ai_items = [a for a in rep["a3b_reviews"] if a.get("judge") == "AI"]
    mixed_items = [a for a in rep["a3b_reviews"] if a.get("verdict")]

    rep["metrics_by_judge"] = {
        "用户口径（user_recheck=true）": {
            "count": len(user_items),
            "citation_support_rate": rate(user_items),
            "task_success_rate": rate(user_items),
            "pending_qids": [a["qid"] for a in user_items if not a.get("verdict")],
        },
        "AI 口径（judge=AI）": {
            "count": len(ai_items),
            "citation_support_rate": rate(ai_items),
            "task_success_rate": rate(ai_items),
        },
        "混合口径（AI + 用户）": {
            "count": len(mixed_items),
            "citation_support_rate": rate(mixed_items),
            "task_success_rate": rate(mixed_items),
        },
    }

    # --- 2. 补齐两个 NOT RUN（用混合口径）---
    mixed_rate = rate(mixed_items)
    rep["metrics"]["citation_support_rate"] = mixed_rate
    rep["metrics"]["task_success_rate"] = mixed_rate
    rep["metrics"]["citation_support_rate_basis"] = (
        f"混合口径 {len(mixed_items)} 条抽检（AI {len(ai_items)} + 用户 {len(user_items)}）"
    )
    rep["metrics"]["task_success_rate_basis"] = (
        "按 verdict 二值判定（支撑 + 引用有效 + 无幻觉）；"
        "unsupported 项如实计失败，不豁免"
    )

    # --- 4. 校准日志（按**本轮实际**生成，不继承上一轮的故事）---
    unsupported = [a["qid"] for a in mixed_items if a["verdict"] == "unsupported"]
    finding = args.calibration_finding or (
        f"人工复核 {len(user_items)} 条抽检，{len(user_items) - len(unsupported)} 条判『支撑』"
        + (f"，{len(unsupported)} 条判『不支撑』（{unsupported}）" if unsupported else "，无『不支撑』")
    )
    rep.setdefault("calibration_log", []).append({
        "at": args.judged_at,
        "by": f"外部审查代判 + 用户委托确认（清单 `{Path(args.review).name}` 头部声明）",
        "action": f"A3b 人工复核回填（{args.version}），补齐 citation_support_rate / task_success_rate",
        "finding": finding,
        "resolution": args.calibration_resolution or (
            "不改数据集、不调阈值。**支持率只覆盖『被抽中的结论句』**，"
            "不等于『每条答案每句都干净』—— 检测器方差与未根治项见报告『残留与已知限制』节。"
        ),
        "scope_note": (
            "本轮支持率口径：5 条人工抽检（citation_risk×2 + multiturn×1 + kb_qa 长答案×2）。"
        ),
    })

    # --- 5. meta 标注 ---
    rep["meta"]["human_review"] = {
        "judge": "user（外部审查代判 + 用户追认）",
        "judged_at": args.judged_at,
        "provenance": provenance or "外部审查代判+用户追认",
        "reviewed_count": len(user_items),
        "verdicts": {a["qid"]: a["verdict"] for a in user_items if a.get("verdict")},
    }
    rep["meta"]["report_version"] = args.version

    # --- 6. 残留与已知限制（原样带进报告，避免"5/5"被读成"已根治"）---
    if args.residual:
        residual_text = Path(args.residual).read_text(encoding="utf-8")
        rep["residual_and_known_limits_md"] = residual_text
        print(f"残留节已并入（{len(residual_text)} 字符）")

    # --- 落盘 ---
    out_json = rep_path.with_name(rep_path.stem + f"_{args.version}.json")
    out_json.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    # Markdown 摘要：从 v1 的 md 改写指标表（避免重复实现渲染器）
    v1_md = rep_path.with_suffix(".md")
    md = render_md_v2(rep, v1_md, version=args.version)
    out_md = out_json.with_suffix(".md")
    out_md.write_text(md, encoding="utf-8")

    print(f"\n报告 {args.version}: {out_json}")
    print(f"摘要 {args.version}: {out_md}")
    print(f"\n三口径支持率：")
    for k, v in rep["metrics_by_judge"].items():
        print(f"  {k}: {v['citation_support_rate']}（{v['count']} 条）")
    print(f"\n引用内容支持率 = {mixed_rate}")
    print(f"任务成功率     = {mixed_rate}")
    return 0


def render_md_v2(rep: dict, v1_md: Path, version: str = "v2") -> str:
    """基于 v1 的 Markdown 改写指标与汇总段落（保持其余结构）。

    报告的**失败项节按本轮实际生成**（不再硬编码"S8 的 m001"故事）；
    若提供了残留节（`residual_and_known_limits_md`），追加为独立章节。
    """
    m = rep["metrics"]
    meta = rep["meta"]
    lines: list[str] = []
    lines.append(f"# OpsPilot 评测报告（{version} · 含 A3b 人工复核回填）")
    lines.append("")
    lines.append(f"- git commit: `{meta['git_commit']}`")
    lines.append(f"- 开始时间: {meta['started_at']}  用时: {meta['duration_s']}s")
    lines.append(f"- 数据集: `{meta['dataset']}`（{meta['total_cases']} 题）")
    lines.append(f"- gold_reviewed: **{meta['gold_reviewed']}**（X5 已确认）")
    hr = meta.get("human_review", {})
    lines.append(f"- 人工复核: **{hr.get('reviewed_count', 0)} 条**（judge={hr.get('judge')}，"
                 f"judged_at={hr.get('judged_at')}）")
    if hr.get("provenance"):
        lines.append(f"  - provenance：{hr['provenance']}")
    lines.append(f"- 参数: top_k={meta['params']['retrieval_top_k']}, "
                 f"min_score={meta['params']['retrieval_min_score']}, "
                 f"ef_search={meta['params']['hnsw_ef_search']}")
    lines.append("")

    def fmt(v):
        if isinstance(v, str):
            return v
        if isinstance(v, float):
            return f"{v:.3f}"
        return str(v)

    def ok(v, thr, lower_better=False):
        if isinstance(v, str):
            return "—"
        return "✅" if (v <= thr if lower_better else v >= thr) else "❌"

    T = rep["metrics"]["thresholds"]
    lines.append(f"## 1. 指标（{version}：两个 `NOT RUN` 已补齐）")
    lines.append("")
    lines.append("| 指标 | 值 | 阈值 | 达标 |")
    lines.append("|---|---|---|---|")
    lines.append(f"| Recall@5 | {fmt(m['recall_at_5'])} | ≥{T['recall_at_5']} | "
                 f"{ok(m['recall_at_5'], T['recall_at_5'])} |")
    lines.append(f"| MRR@5 | {fmt(m['mrr_at_5'])} | ≥{T['mrr_at_5']} | "
                 f"{ok(m['mrr_at_5'], T['mrr_at_5'])} |")
    lines.append(f"| **引用内容支持率** | **{fmt(m['citation_support_rate'])}** | "
                 f"≥{T['citation_support_rate']} | {ok(m['citation_support_rate'], T['citation_support_rate'])} |")
    lines.append(f"| 拒答正确率 | {fmt(m['refusal_accuracy'])} | ≥{T['refusal_accuracy']} | "
                 f"{ok(m['refusal_accuracy'], T['refusal_accuracy'])} |")
    lines.append(f"| 过度拒答率 | {fmt(m['over_refusal_rate'])} | ≤{T['over_refusal_rate']} | "
                 f"{ok(m['over_refusal_rate'], T['over_refusal_rate'], True)} |")
    lines.append(f"| **任务成功率** | **{fmt(m['task_success_rate'])}** | "
                 f"≥{T['task_success_rate']} | {ok(m['task_success_rate'], T['task_success_rate'])} |")
    lines.append(f"| 多轮保持率 | {fmt(m['multiturn_retention'])} | ≥{T['multiturn_retention']} | "
                 f"{ok(m['multiturn_retention'], T['multiturn_retention'])} |")
    lines.append("")
    lat = m["latency"]
    lines.append(f"**延迟**：kb_qa P50={lat['kb_qa_p50_ms']}ms / P95={lat['kb_qa_p95_ms']}ms"
                 f"（≤{T['latency_kb_qa_p95_ms']}ms）；"
                 f"log_diag P50={lat['log_diag_p50_ms']}ms / P95={lat['log_diag_p95_ms']}ms"
                 f"（≤{T['latency_log_diag_p95_ms']}ms）")
    lines.append("")
    lines.append(f"**引用 ID 有效率（硬线，独立于 7 项）**：{fmt(m['citation_id_validity'])} "
                 f"{'✅ 全通过' if m['citation_id_validity_all_pass'] else '❌ 存在无效引用'}"
                 f" —— 注意：它只证明「引用存在」，**不证明「引用支撑结论」**，"
                 f"后者是下面 §2 的人工复核职责。")
    lines.append("")
    lines.append("## 2. 分 judge 汇总（禁止只给合并值）")
    lines.append("")
    lines.append("| 口径 | 条数 | 引用内容支持率 | 任务成功率 |")
    lines.append("|---|---|---|---|")
    for k, v in rep["metrics_by_judge"].items():
        lines.append(f"| {k} | {v['count']} | {fmt(v['citation_support_rate'])} | "
                     f"{fmt(v['task_success_rate'])} |")
    lines.append("")

    # 不支撑明细（按本轮实际生成）
    un = [a for a in rep["a3b_reviews"] if a.get("verdict") == "unsupported"]
    if un:
        lines.append("### ⚠️ 人工复核发现的失败项（**这条最有价值**）")
        lines.append("")
        lines.append("| 题号 | 类别 | 说明 |")
        lines.append("|---|---|---|")
        for a in un:
            lines.append(f"| `{a['qid']}` | {a.get('category')} | "
                         f"片段不足以支撑该结论（引用 ID 有效，机器判过，但**内容不支撑**） |")
        lines.append("")
        lines.append("> 这条证明了两件事：")
        lines.append("> ① 机器判定的「引用 ID 有效率 100%」**不足以**保证引用可用 —— "
                     "它只验证 ID 存在，不验证语义支撑；")
        lines.append("> ② A3b 这道人工工序**不是走过场** —— 若只看自动指标，这个问题会被完全漏掉。")
        lines.append("")
    else:
        lines.append("### 本轮人工抽检：5/5 判「支撑」")
        lines.append("")
        lines.append("> ⚠️ **但这不是「引用越界已根治」** —— 支持率只覆盖「被抽中的结论句」，"
                     "不等于「每条答案每句都干净」。**未根治项与检测器方差见下方"
                     "『残留与已知限制』章节**。")
        lines.append("")

    lines.append("## 3. 校准日志")
    lines.append("")
    for c in rep.get("calibration_log", []):
        lines.append(f"- **{c['at']}**（{c['by']}）：{c['action']}")
        lines.append(f"  - 发现：{c['finding']}")
        lines.append(f"  - 处置：{c['resolution']}")
        ai = c.get("action_item")
        if ai:
            lines.append(f"  - 行动项 `{ai['id']}`（{ai['scope']}，{ai['due']}）：{ai['change']}")
    lines.append("")
    if rep.get("residual_and_known_limits_md"):
        lines.append("## 4. 残留与已知限制（原样保留，勿删）")
        lines.append("")
        lines.append(rep["residual_and_known_limits_md"].strip())
        lines.append("")
    lines.append("## 5. 逐题结果")
    lines.append("")
    lines.append("| 题号 | 类别 | 期望 | 实际 | Recall@5 | MRR@5 | 引用 | 延迟ms | 工具 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for c in rep["cases"]:
        lines.append(
            f"| {c['qid']} | {c['category']} | {c['expected_answer_type']} | {c['got_answer_type']} | "
            f"{fmt(c['recall_at_5'])} | {fmt(c['mrr_at_5'])} | {c['citation_count']} | "
            f"{c['latency_ms']} | {','.join(c['tool_calls']) or '—'} |"
        )
    lines.append("")
    lines.append("> 未跑指标一律标 `NOT RUN`，未做推算填充（A12）。")
    _ = v1_md
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())