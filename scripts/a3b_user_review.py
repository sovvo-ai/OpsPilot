"""生成 a3b_user_review.md —— 用户人工复核清单（D1 之后的第 4 项）。

用途：把 A3b 抽检里 `user_recheck=true` 的 5 条，渲染成"四件套"清单，
用户打勾即可；后续由 `apply_user_review()` 回填报告重算汇总。

四件套（每条）：
  1. 【问题】原始问句
  2. 【结论句】模型答案中**引用了片段的那句**（不是整段答案）
  3. 【引用片段】被引用 chunk 的**原文**（从 DB 按 chunk_id 取）
  4. ☐ 支撑 / ☐ 不支撑  —— **判据：该片段是否支撑那句具体结论**，
     不是"片段与问题相关不相关"（citation_risk 的 q028/q029 专测这个区别）

用法：
    ai-service/.venv/Scripts/python.exe scripts/a3b_user_review.py \
        --report ai-service/evals/reports/<ts>.json \
        --out docs/testing/a3b_user_review.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / "deploy" / ".env"


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


def fetch_chunks(dsn: str, chunk_ids: list[str]) -> dict[str, dict]:
    """按 chunk_id 批量取内容与来源文档标题。"""
    out: dict[str, dict] = {}
    if not chunk_ids:
        return out
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.chunk_id, c.content, c.heading_path, d.title AS doc_title
                     FROM doc_chunks c
                     LEFT JOIN documents d ON d.id = c.doc_id
                    WHERE c.chunk_id = ANY(%s)""",
                (chunk_ids,),
            )
            for row in cur.fetchall():
                out[str(row["chunk_id"])] = row
    return out


SENT_SPLIT = re.compile(r"(?<=[。！？；\n])")


def pick_supported_sentence(text: str, cite_marker: str) -> str:
    """挑出**含该引用的那句结论**（而不是整段答案）。

    报告里只存了 `final_text_head`（前 200 字），若引用标记不在其中，
    退回"最后一句含引用的句子"或整段头部。
    """
    if not text:
        return "（答案文本缺失）"
    # 优先：含 `[n]` 标记的句子
    for sent in SENT_SPLIT.split(text):
        if cite_marker and cite_marker in sent:
            return sent.strip()
    return text[:160].strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True, help="评测报告 JSON 路径")
    ap.add_argument("--out", default=str(PROJECT_ROOT / "docs" / "testing" / "a3b_user_review.md"))
    args = ap.parse_args()

    env = load_env()
    os.environ.update({k: v for k, v in env.items() if k not in os.environ})
    dsn = build_dsn(env)

    rep = json.loads(Path(args.report).read_text(encoding="utf-8"))
    pending = [a for a in rep["a3b_reviews"] if a.get("user_recheck")]
    cases = {c["qid"]: c for c in rep["cases"]}

    # 收集所有待复核条目的引用 chunk_id
    all_cids: list[str] = []
    for a in pending:
        c = cases.get(a["qid"])
        if c:
            all_cids.extend(c.get("retrieved_chunk_ids") or [])
    chunks = fetch_chunks(dsn, list(set(all_cids)))

    lines: list[str] = []
    lines.append("# A3b 人工复核清单（待你打勾）")
    lines.append("")
    lines.append(f"- 报告：`{Path(args.report).name}`")
    lines.append(f"- git commit：`{rep['meta'].get('git_commit')}`")
    lines.append(f"- 待复核：**{len(pending)} 条**（`user_recheck=true`）")
    lines.append("")
    lines.append("## 怎么判（重要）")
    lines.append("")
    lines.append("判断标准是 **「这段引用是否支撑那句具体结论」**，而不是「片段与问题相关不相关」。")
    lines.append("")
    lines.append("反例：片段在讲 Nginx 超时，结论句也提到 Nginx 超时 —— 看似相关；")
    lines.append("但如果结论说的是「默认 30 秒」而片段写的是「默认 60 秒」，就是**不支撑**。")
    lines.append("")
    lines.append("> 提醒：`q028`（`proxy_read_timeout`）与 `q029`（`long_query_time`）是**专测这个区别**的引用风险题。")
    lines.append("")
    lines.append("填法：在每条的 `判定：` 行把 `[ ] 支撑` 或 `[ ] 不支撑` 改成 `[x]`，保存即可。")
    lines.append("")
    lines.append("---")
    lines.append("")

    for i, a in enumerate(pending, 1):
        qid = a["qid"]
        c = cases.get(qid, {})
        lines.append(f"## {i}. `{qid}` · {a.get('category')}")
        lines.append("")
        lines.append(f"**抽样理由**：{a.get('reason','')}")
        lines.append("")
        lines.append(f"**① 问题**")
        lines.append("")
        lines.append(f"> {c.get('question','（缺失）')}")
        lines.append("")

        cits = c.get("citations") or []
        lines.append(f"**② 结论句**（答案中引用片段的那句）")
        lines.append("")
        # 优先用完整 final_text（D2 起报告已含），回落 head
        full = c.get("final_text") or c.get("final_text_head") or ""
        marker = f"[{cits[0].get('index', 1)}]" if cits else ""
        lines.append(f"> {pick_supported_sentence(full, marker)}")
        lines.append("")

        lines.append(f"**③ 引用片段原文**")
        lines.append("")
        for j, cit in enumerate(cits, 1):
            cid = str(cit.get("chunk_id") or "")
            row = chunks.get(cid)
            title = row.get("doc_title") if row else cit.get("doc_title", "?")
            body = (row.get("content") if row else None) or "（片段内容未取到 —— 请确认 DB 可用）"
            body = body.strip()
            if len(body) > 700:
                body = body[:700] + " …（截断）"
            # ⚠️ **必须用 citations[].index，不能用 enumerate 的位置**（q029 瑕疵修复）。
            # 契约：正文角标 `[n]` 里的 n === citations[].index。
            # 早期这里写 `引用 {j}`（数组位置），于是正文写 `[3]` 的条目在清单里显示"引用 1"
            # —— 复核者按清单标号回查正文会对不上（`a3b_user_review_v3.md` q029 已记录）。
            idx = cit.get("index")
            label = f"引用 {idx}" if idx else f"引用 {j}（无 index 字段，按位置）"
            lines.append(f"*{label} · 来源 `{title}` · chunk `{cid[:8]}…`*")
            lines.append("")
            lines.append(f"> {body.replace(chr(10), chr(10) + '> ')}")
            lines.append("")

        lines.append("**④ 判定**")
        lines.append("")
        lines.append("```")
        lines.append("判定：[ ] 支撑   [ ] 不支撑")
        lines.append("备注：（可选）")
        lines.append("```")
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.append("## 填完之后")
    lines.append("")
    lines.append("告诉我一声，我会：")
    lines.append("")
    lines.append("1. 把判定回填进报告 `a3b_reviews`（`judge=\"user\"`、`judged_at=<时间>`、`verdict=supported/unsupported`）")
    lines.append("2. 重算**用户口径**与**混合口径**的「引用内容支持率」")
    lines.append("3. 按 `verdict` 重算「任务成功率」（支撑 + 引用有效 + 无幻觉）")
    lines.append("4. 出报告 v2，补齐那两个 `NOT RUN`")
    lines.append("")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"已生成: {out}")
    print(f"待复核 {len(pending)} 条: {[a['qid'] for a in pending]}")
    missing = [cid for cid in set(all_cids) if cid not in chunks]
    if missing:
        print(f"⚠️ 有 {len(missing)} 个 chunk 未取到内容（DB 不可用？）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())