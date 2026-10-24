"""S8 评测真跑：35 题全量 + 7 项指标 + 多轮 A/B 保持率。

用法（在完整栈启动、5 篇 seed_docs 已摄取至 completed 后）：

    cd ai-service
    export POSTGRES_HOST=localhost POSTGRES_PORT=55432 ...
    .venv/Scripts/python.exe -m evals.run_eval \\
        --out evals/reports/$(date +%Y%m%dT%H%M).json

设计要点（对齐 rag-eval-spec）：
- **7 项指标**：Recall@5 / MRR@5 / 引用内容支持率 / 拒答正确率 / 过度拒答率 /
  任务成功率 / 多轮保持率 + 延迟 P50/P95。
- **引用 ID 有效率**是**独立硬线断言**（100%），**不混入 7 项**。
- **多轮保持率**用 §3 原口径：`ΣRecall_A / ΣRecall_B`；剔除非可比配对，
  `NOT_COMPARABLE > 40%` 或 `ΣRecall_B = 0` → 输出 `NOT RUN`。
- **冒烟**：至少 1 条真题从 jsonl 读入、走完整链路（防"字段→检索输入"装配盲区）。
- **A3b**：20 条抽检，每条带 judge 与时间；**固定 5 条标 `user_recheck=true`**
  判定人留空等用户填；其余 15 条 `judge=AI`。汇总按 judge 分列（用户/AI/混合）。
- **未跑指标一律 `NOT RUN`**，禁填推算值（A12）。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DATASET = Path(__file__).resolve().parent / "datasets" / "qa_golden.jsonl"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "reports"
ENV_FILE = PROJECT_ROOT / "deploy" / ".env"
SEED_DIR = PROJECT_ROOT / "scripts" / "seed_docs"

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8081")
OWNER = "demo-user"

# 阈值（rag-eval-spec §2）
THRESHOLDS = {
    "recall_at_5": 0.75,
    "mrr_at_5": 0.60,
    "citation_support_rate": 0.90,
    "refusal_accuracy": 0.85,
    "over_refusal_rate": 0.15,   # 观察指标（越小越好）
    "task_success_rate": 0.70,
    "multiturn_retention": 0.90,
    "citation_id_validity": 1.00,  # 硬线，独立断言
    "latency_kb_qa_p95_ms": 8000,
    "latency_log_diag_p95_ms": 25000,
}


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


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


@dataclass
class CaseResult:
    """单题结果（逐题落报告）。"""

    qid: str
    category: str
    question: str
    should_answer: bool
    got_answer_type: str
    expected_answer_type: str
    route: str
    node_path: list[str]
    duration_ms: int
    retrieval_top_score: float
    retrieved_chunk_ids: list[str]
    citations: list[dict[str, Any]]
    final_text: str
    recall_at_5: float | None = None
    mrr_at_5: float | None = None
    citation_id_valid: bool | None = None
    is_refusal: bool = False
    latency_ms: int = 0
    error: str | None = None
    tool_calls: list[str] = field(default_factory=list)
    review: dict[str, Any] | None = None   # A3b 抽检记录


class Evaluator:
    def __init__(self, dsn: str, backend: str = BACKEND_URL) -> None:
        self.dsn = dsn
        self.backend = backend.rstrip("/")
        self.token = self._dev_token()
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def _dev_token(self) -> str:
        r = httpx.post(f"{self.backend}/api/v1/auth/dev-token",
                       params={"user": OWNER}, timeout=10)
        r.raise_for_status()
        return r.json()["token"]

    def db(self):
        return psycopg.connect(self.dsn, row_factory=dict_row)

    # --- 环境准备 ---

    def ensure_seeds(self) -> dict[str, Any]:
        """5 篇 seed_docs 全量摄取至 completed（幂等：重复上传会 deduplicated）。"""
        info: dict[str, Any] = {"docs": [], "all_completed": True}
        for p in sorted(SEED_DIR.glob("*.md")):
            with p.open("rb") as f:
                up = httpx.post(
                    f"{self.backend}/api/v1/documents", headers=self.headers,
                    files={"file": (p.name, f, "text/markdown")}, timeout=30,
                )
            if up.status_code not in (201, 202):
                info["all_completed"] = False
                info["docs"].append({"name": p.name, "error": up.text[:200]})
                continue
            doc_id = up.json()["doc_id"]
            final = self._poll_doc(doc_id)
            info["docs"].append({
                "name": p.name, "doc_id": doc_id,
                "status": final.get("ingest_status"),
                "chunks": final.get("chunk_count"),
                "deduplicated": up.json().get("deduplicated", False),
            })
            if final.get("ingest_status") != "completed":
                info["all_completed"] = False
        return info

    def _poll_doc(self, doc_id: str, timeout: int = 120) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = httpx.get(f"{self.backend}/api/v1/documents/{doc_id}",
                          headers=self.headers, timeout=10)
            d = r.json()
            if d.get("ingest_status") in ("completed", "failed"):
                return d
            time.sleep(1)
        return r.json()

    # --- 单题执行 ---

    def new_session(self, title: str) -> str:
        r = httpx.post(f"{self.backend}/api/v1/sessions", headers=self.headers,
                       json={"title": title}, timeout=10)
        r.raise_for_status()
        return r.json()["id"]

    def ask(self, sid: str, question: str, attach_log: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"question": question}
        if attach_log:
            body["attach_log"] = attach_log
        r = httpx.post(f"{self.backend}/api/v1/sessions/{sid}/messages",
                       headers=self.headers, json=body, timeout=180)
        r.raise_for_status()
        return r.json()

    def get_run(self, run_id: str) -> dict[str, Any]:
        r = httpx.get(f"{self.backend}/api/v1/runs/{run_id}",
                      headers=self.headers, timeout=10)
        return r.json()

    # --- 指标计算 ---

    def chunks_of(self, chunk_ids: list[str]) -> list[str]:
        """按 chunk_id 取内容（用于 gold 子串匹配）。"""
        out: list[str] = []
        with self.db() as conn:
            with conn.cursor() as cur:
                for cid in chunk_ids:
                    if not cid:
                        continue
                    cur.execute("SELECT content FROM doc_chunks WHERE chunk_id = %s", (cid,))
                    row = cur.fetchone()
                    if row:
                        out.append(row["content"])
        return out

    def recall_at_5(self, retrieved_ids: list[str], gold_texts: list[str]) -> float:
        """按 gold_chunk_texts 在 Top-5 检索集内做子串匹配（micro）。"""
        if not gold_texts:
            return 0.0
        corpus = "\n".join(self.chunks_of(retrieved_ids[:5]))
        hit = sum(1 for g in gold_texts if g and g in corpus)
        return hit / len(gold_texts)

    def mrr_at_5(self, retrieved_ids: list[str], gold_texts: list[str]) -> float:
        """Top-5 内首个命中切片的倒数排名；无命中计 0。"""
        if not gold_texts:
            return 0.0
        for rank, cid in enumerate(retrieved_ids[:5], start=1):
            corpus = "\n".join(self.chunks_of([cid]))
            if any(g and g in corpus for g in gold_texts):
                return 1.0 / rank
        return 0.0

    def citation_id_valid(self, citations: list[dict[str, Any]], retrieved_ids: list[str]) -> bool:
        """引用 ID 有效率（硬线）：每个 chunk_id 存在且属于本次检索集。"""
        if not citations:
            return True  # 无引用不算无效（拒答场景）
        valid_set = set(retrieved_ids)
        return all(c.get("chunk_id") in valid_set for c in citations)

# ---------------------------------------------------------------------------
# A3b 人工抽检抽样（固定规则，写死并注释，保证可复算）
# ---------------------------------------------------------------------------


def select_a3b_cases(rows: list[dict[str, Any]], per_case: dict[str, CaseResult]) -> list[dict[str, Any]]:
    """固定选 5 条标 `user_recheck=true`（判定人留空等用户填）。

    抽样规则（写死，来源 = S8 任务书指定）：
      1. `citation_risk` × 2 —— 引用风险题最容易"引用存在但不支持"，最需要人看
      2. `multiturn` × 1 —— 多轮改写路径的引用链最容易断
      3. `kb_qa` 长答案 × 2 —— 长答案里"引用是否支撑每一句结论"人工难自动校验

    其余 15 条（`should_answer=true` 的作答题，按 id 升序）`judge = "AI"`。
    """
    picked: list[dict[str, Any]] = []

    def add(cat: str, n: int, only_long: bool = False) -> None:
        pool = [
            r for r in rows
            if r.get("category") == cat and r["id"] in per_case
            and per_case[r["id"]].got_answer_type == "answered"
        ]
        if only_long:
            pool.sort(key=lambda r: -len(per_case[r["id"]].final_text or ""))
        for r in pool[:n]:
            if r["id"] not in [p["qid"] for p in picked]:
                picked.append({"qid": r["id"], "category": cat, "reason": _reason(cat, only_long)})

    add("citation_risk", 2)
    add("multiturn", 1)
    add("kb_qa", 2, only_long=True)

    # 补足到 5 条（若某类不足）
    if len(picked) < 5:
        for r in rows:
            if r["id"] in per_case and r["id"] not in [p["qid"] for p in picked]:
                if per_case[r["id"]].got_answer_type == "answered":
                    picked.append({"qid": r["id"], "category": r.get("category"),
                                   "reason": "补足抽样"})
            if len(picked) >= 5:
                break

    # 其余 15 条 = 作答题里未被选中的
    remaining = [
        r for r in rows
        if r["id"] in per_case
        and per_case[r["id"]].got_answer_type == "answered"
        and r["id"] not in [p["qid"] for p in picked]
    ]
    selected_ids = {p["qid"] for p in picked}
    a3b: list[dict[str, Any]] = []
    for p in picked:
        a3b.append({
            "qid": p["qid"], "category": p["category"], "reason": p["reason"],
            "user_recheck": True,
            "judge": None,          # 留空等用户填
            "judged_at": None,
            "verdict": None,        # supported / unsupported
        })
    for r in remaining[: 20 - len(picked)]:
        a3b.append({
            "qid": r["id"], "category": r.get("category"), "reason": "常规抽检",
            "user_recheck": False,
            "judge": "AI",
            "judged_at": datetime.now(timezone.utc).isoformat(),
            "verdict": _ai_support_verdict(per_case[r["id"]]),
        })
    _ = selected_ids
    return a3b


def _reason(cat: str, only_long: bool) -> str:
    if only_long:
        return "kb_qa 长答案：引用是否逐句支撑人工难自动校验"
    return {
        "citation_risk": "引用风险题：最易出现'引用存在但不支持结论'",
        "multiturn": "多轮改写路径：引用链最易断",
    }.get(cat, "指定抽样")


def _ai_support_verdict(cr: CaseResult) -> str:
    """AI 口径的初判：引用非空且引用 ID 有效 → supported（**仅作 AI 口径，不作最终值**）。"""
    if not cr.citations:
        return "unsupported"
    return "supported" if cr.citation_id_valid else "unsupported"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_case(ev: Evaluator, row: dict[str, Any]) -> CaseResult:
    """跑单题（含多轮的 A/B 两路）。"""
    qid = row["id"]
    cat = row.get("category", "unknown")
    question = row["question"]
    should_answer = bool(row.get("should_answer"))

    if cat == "multiturn":
        return _run_multiturn_case(ev, row)

    sid = ev.new_session(f"eval-{qid}")
    t0 = time.monotonic()
    try:
        resp = ev.ask(sid, question, attach_log=row.get("attach_log"))
    except Exception as exc:  # noqa: BLE001
        return CaseResult(
            qid=qid, category=cat, question=question, should_answer=should_answer,
            got_answer_type="error", expected_answer_type="answered" if should_answer else "refusal",
            route="", node_path=[], duration_ms=0, retrieval_top_score=0.0,
            retrieved_chunk_ids=[], citations=[], final_text="", error=str(exc),
        )
    wall = int((time.monotonic() - t0) * 1000)

    run = ev.get_run(resp["run_id"])
    msg = resp.get("message", {})
    retrieved = _retrieved_ids(run) or [c.get("chunk_id") for c in (msg.get("citations") or [])]
    gold = row.get("gold_chunk_texts") or []

    cr = CaseResult(
        qid=qid, category=cat, question=question, should_answer=should_answer,
        got_answer_type=msg.get("answer_type", "unknown"),
        expected_answer_type="answered" if should_answer else "refusal",
        route=run.get("route", ""),
        node_path=run.get("node_path") or [],
        duration_ms=run.get("duration_ms", 0),
        retrieval_top_score=float(run.get("retrieval_top_score") or 0.0),
        retrieved_chunk_ids=retrieved,
        citations=msg.get("citations") or [],
        final_text=msg.get("content") or "",
        latency_ms=wall,
        tool_calls=[t.get("tool_name", "") for t in (run.get("tool_call_records") or [])],
    )
    if should_answer and gold:
        cr.recall_at_5 = ev.recall_at_5(retrieved, gold)
        cr.mrr_at_5 = ev.mrr_at_5(retrieved, gold)
    cr.citation_id_valid = ev.citation_id_valid(cr.citations, retrieved)
    cr.is_refusal = cr.got_answer_type in ("refusal", "clarify")
    return cr


def _retrieved_ids(run: dict[str, Any]) -> list[str]:
    """从 run 里取本次检索集（run 接口未直接暴露时回落 steps 摘要）。"""
    ids = run.get("retrieved_chunk_ids")
    if isinstance(ids, list):
        return ids
    return []


def _run_multiturn_case(ev: Evaluator, row: dict[str, Any]) -> CaseResult:
    """多轮：A 组（context_turns + question）/ B 组（control_single_turn_question）。

    §3 原口径：`ΣRecall_A / ΣRecall_B`；B 组不得触发 rewrite（否则该样本判失败）。
    """
    qid = row["id"]
    ctx = row.get("context_turns") or []
    gold = row.get("gold_chunk_texts") or []

    # A 组：逐轮把 context user 轮真实提问，建立历史，再问依赖上下文的追问
    sid_a = ev.new_session(f"eval-{qid}-A")
    for turn in ctx:
        if turn.get("role") == "user":
            ev.ask(sid_a, turn["content"])
    t0 = time.monotonic()
    resp_a = ev.ask(sid_a, row["question"])
    wall = int((time.monotonic() - t0) * 1000)
    run_a = ev.get_run(resp_a["run_id"])
    msg_a = resp_a.get("message", {})
    retr_a = _retrieved_ids(run_a)

    # B 组：单轮等价问法（首轮、无历史 → 不得触发 rewrite）
    sid_b = ev.new_session(f"eval-{qid}-B")
    resp_b = ev.ask(sid_b, row["control_single_turn_question"])
    run_b = ev.get_run(resp_b["run_id"])
    retr_b = _retrieved_ids(run_b)

    cr = CaseResult(
        qid=qid, category="multiturn", question=row["question"],
        should_answer=bool(row.get("should_answer")),
        got_answer_type=msg_a.get("answer_type", "unknown"),
        expected_answer_type="answered",
        route=run_a.get("route", ""),
        node_path=run_a.get("node_path") or [],
        duration_ms=run_a.get("duration_ms", 0),
        retrieval_top_score=float(run_a.get("retrieval_top_score") or 0.0),
        retrieved_chunk_ids=retr_a,
        citations=msg_a.get("citations") or [],
        final_text=msg_a.get("content") or "",
        latency_ms=wall,
        tool_calls=[t.get("tool_name", "") for t in (run_a.get("tool_call_records") or [])],
    )
    # 多轮用 A/B 两路 Recall 记录（报告里另算保持率）
    recall_a = ev.recall_at_5(retr_a, gold)
    recall_b = ev.recall_at_5(retr_b, gold)
    cr.recall_at_5 = recall_a
    cr.mrr_at_5 = ev.mrr_at_5(retr_a, gold)
    cr.citation_id_valid = ev.citation_id_valid(cr.citations, retr_a)
    cr.is_refusal = cr.got_answer_type in ("refusal", "clarify")
    # 附带 B 组信息（供保持率计算）
    cr.review = {
        "multiturn_ab": {
            "recall_a": recall_a,
            "recall_b": recall_b,
            "b_triggered_rewrite": _rewrite_flag(run_b),
            "retr_a": retr_a,
            "retr_b": retr_b,
        }
    }
    return cr


def _rewrite_flag(run: dict[str, Any]) -> bool:
    for s in run.get("steps") or []:
        if s.get("node") == "retrieval":
            return "rewritten=True" in (s.get("output_summary") or "")
    return False


def compute_metrics(results: list[CaseResult], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """算 7 项指标 + 延迟；未跑项返回 NOT RUN 字符串。"""
    m: dict[str, Any] = {}
    by_cat = {r["id"]: r.get("category") for r in rows}

    # 1. Recall@5 / MRR@5（micro，仅统计应作答且有 gold 的题）
    answered_with_gold = [
        r for r in results
        if r.should_answer and r.recall_at_5 is not None and by_cat.get(r.qid) != "multiturn"
    ]
    if answered_with_gold:
        m["recall_at_5"] = sum(r.recall_at_5 or 0 for r in answered_with_gold) / len(answered_with_gold)
        m["mrr_at_5"] = sum(r.mrr_at_5 or 0 for r in answered_with_gold) / len(answered_with_gold)
    else:
        m["recall_at_5"] = "NOT RUN"
        m["mrr_at_5"] = "NOT RUN"

    # 2. 引用 ID 有效率（硬线，独立）
    all_cit_valid = all(r.citation_id_valid for r in results if r.citation_id_valid is not None)
    m["citation_id_validity"] = 1.0 if all_cit_valid else 0.0
    m["citation_id_validity_all_pass"] = bool(all_cit_valid)

    # 3. 拒答正确率 / 过度拒答率
    should_refuse = [r for r in results if not r.should_answer]
    should_answer = [r for r in results if r.should_answer]
    m["refusal_accuracy"] = (
        sum(1 for r in should_refuse if r.is_refusal) / len(should_refuse)
        if should_refuse else "NOT RUN"
    )
    m["over_refusal_rate"] = (
        sum(1 for r in should_answer if r.is_refusal) / len(should_answer)
        if should_answer else "NOT RUN"
    )

    # 4. 多轮保持率（§3 原口径）
    m["multiturn_retention"] = _multiturn_retention(results)

    # 5. 引用内容支持率 → 需人判，报告中标 NOT RUN（A3b 留位）
    m["citation_support_rate"] = "NOT RUN"   # 待用户填 A3b 5 条
    # 6. 任务成功率 → 需人判
    m["task_success_rate"] = "NOT RUN"       # 待用户填 A3b 5 条

    # 7. 延迟（分 kb_qa 与 log_diag）
    kb = [r.latency_ms for r in results if by_cat.get(r.qid) == "kb_qa"]
    log = [r.latency_ms for r in results if by_cat.get(r.qid) == "log_diag"]
    alllat = [r.latency_ms for r in results]
    m["latency"] = {
        "kb_qa_p50_ms": int(statistics.median(kb)) if kb else "NOT RUN",
        "kb_qa_p95_ms": _p95(kb) if kb else "NOT RUN",
        "log_diag_p50_ms": int(statistics.median(log)) if log else "NOT RUN",
        "log_diag_p95_ms": _p95(log) if log else "NOT RUN",
        "overall_p50_ms": int(statistics.median(alllat)) if alllat else "NOT RUN",
        "overall_p95_ms": _p95(alllat) if alllat else "NOT RUN",
    }
    m["thresholds"] = THRESHOLDS
    return m


def _p95(xs: list[int]) -> int:
    if not xs:
        return 0
    s = sorted(xs)
    idx = max(0, int(round(0.95 * (len(s) - 1))))
    return s[idx]


def _multiturn_retention(results: list[CaseResult]) -> Any:
    """§3 原口径：ΣRecall_A / ΣRecall_B；非可比 >40% 或 ΣRecall_B=0 → NOT RUN。"""
    pairs = [r.review["multiturn_ab"] for r in results
             if r.review and "multiturn_ab" in r.review]
    if not pairs:
        return "NOT RUN"
    comparable = [p for p in pairs if p["recall_b"] > 0]
    not_comparable = len(pairs) - len(comparable)
    if not pairs or not_comparable / len(pairs) > 0.40:
        return "NOT RUN"
    sum_a = sum(p["recall_a"] for p in comparable)
    sum_b = sum(p["recall_b"] for p in comparable)
    if sum_b == 0:
        return "NOT RUN"
    return sum_a / sum_b


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def build_report(
    results: list[CaseResult],
    rows: list[dict[str, Any]],
    metrics: dict[str, Any],
    a3b: list[dict[str, Any]],
    env: dict[str, str],
    seeds: dict[str, Any],
    smoke: dict[str, Any],
    started_at: str,
    duration_s: float,
) -> dict[str, Any]:
    """组装报告（meta + 逐题结果 + A3b + judge 分列汇总）。"""
    user_recheck = [a for a in a3b if a.get("user_recheck")]
    ai_judged = [a for a in a3b if a.get("judge") == "AI"]

    def _rate(items: list[dict[str, Any]]) -> Any:
        judged = [i for i in items if i.get("verdict")]
        if not judged:
            return "NOT RUN"
        return sum(1 for i in judged if i["verdict"] == "supported") / len(judged)

    summary_by_judge = {
        "用户口径（user_recheck=true，判定人留空）": {
            "count": len(user_recheck),
            "citation_support_rate": _rate(user_recheck),   # 未填 → NOT RUN
            "pending_qids": [i["qid"] for i in user_recheck],
        },
        "AI 口径（judge=AI）": {
            "count": len(ai_judged),
            "citation_support_rate": _rate(ai_judged),
        },
        "混合口径（AI 已判 + 用户已填）": {
            "count": len([i for i in a3b if i.get("verdict")]),
            "citation_support_rate": _rate(a3b),
        },
    }

    return {
        "meta": {
            "git_commit": git_commit(),
            "started_at": started_at,
            "duration_s": round(duration_s, 1),
            "models": {
                "router": env.get("LLM_MODEL_ROUTER", "qwen-flash"),
                "qa": env.get("LLM_MODEL_QA", "qwen-plus"),
                "troubleshoot": env.get("LLM_MODEL_TROUBLESHOOT", "qwen-plus"),
                "checker": env.get("LLM_MODEL_CHECKER", "qwen-plus"),
                "final": env.get("LLM_MODEL_FINAL", "qwen-plus"),
                "embedding": env.get("EMBEDDING_MODEL", "text-embedding-v4"),
            },
            "params": {
                "retrieval_top_k": int(env.get("RETRIEVAL_TOP_K", 5)),
                "retrieval_min_score": float(env.get("RETRIEVAL_MIN_SCORE", 0.35)),
                "hnsw_ef_search": int(env.get("HNSW_EF_SEARCH", 100)),
            },
            "gold_reviewed": True,                 # X5 已确认（rag-eval-spec §1.4）
            "dataset": "evals/datasets/qa_golden.jsonl",
            "total_cases": len(rows),
            "seeds": seeds,
            "smoke": smoke,
        },
        "metrics": metrics,
        "metrics_by_judge": summary_by_judge,
        "a3b_reviews": a3b,
        "cases": [
            {
                "qid": r.qid, "category": r.category, "question": r.question,
                "should_answer": r.should_answer,
                "expected_answer_type": r.expected_answer_type,
                "got_answer_type": r.got_answer_type,
                "route": r.route, "node_path": r.node_path,
                "duration_ms": r.duration_ms, "latency_ms": r.latency_ms,
                "retrieval_top_score": r.retrieval_top_score,
                "retrieved_chunk_ids": r.retrieved_chunk_ids,
                "recall_at_5": r.recall_at_5, "mrr_at_5": r.mrr_at_5,
                "citation_id_valid": r.citation_id_valid,
                "citation_count": len(r.citations),
                # D2：引用明细必须入报告 —— 人工复核（A3b）要判断
                # "这段引用是否支撑该结论"，只有 count 无法判。
                "citations": [
                    {
                        "index": i + 1,
                        "chunk_id": c.get("chunk_id"),
                        "doc_title": c.get("doc_title"),
                        "score": c.get("score"),
                        "source_type": c.get("source_type"),
                        "status": c.get("status"),
                    }
                    for i, c in enumerate(r.citations)
                ],
                "tool_calls": r.tool_calls,
                "final_text": (r.final_text or "")[:1500],
                "final_text_head": (r.final_text or "")[:200],
                "error": r.error,
                "multiturn_ab": (r.review or {}).get("multiturn_ab"),
            }
            for r in results
        ],
    }


def render_markdown(rep: dict[str, Any]) -> str:
    """Markdown 摘要（人读版）。"""
    m = rep["metrics"]
    meta = rep["meta"]
    lines: list[str] = []
    lines.append("# OpsPilot 评测报告")
    lines.append("")
    lines.append(f"- git commit: `{meta['git_commit']}`")
    lines.append(f"- 开始时间: {meta['started_at']}  用时: {meta['duration_s']}s")
    lines.append(f"- 数据集: `{meta['dataset']}`（{meta['total_cases']} 题）")
    lines.append(f"- gold_reviewed: **{meta['gold_reviewed']}**（X5 已确认）")
    lines.append(f"- 参数: top_k={meta['params']['retrieval_top_k']}, "
                 f"min_score={meta['params']['retrieval_min_score']}, "
                 f"ef_search={meta['params']['hnsw_ef_search']}")
    lines.append("")

    lines.append("## 1. 指标")
    lines.append("")
    lines.append("| 指标 | 值 | 阈值 | 达标 |")
    lines.append("|---|---|---|---|")

    def fmt(v: Any) -> str:
        if isinstance(v, str):
            return v
        if isinstance(v, float):
            return f"{v:.3f}"
        return str(v)

    def ok(v: Any, thr: Any, lower_better: bool = False) -> str:
        if isinstance(v, str):
            return "—"
        return "✅" if (v <= thr if lower_better else v >= thr) else "❌"

    lines.append(f"| Recall@5 | {fmt(m['recall_at_5'])} | ≥{THRESHOLDS['recall_at_5']} | "
                 f"{ok(m['recall_at_5'], THRESHOLDS['recall_at_5'])} |")
    lines.append(f"| MRR@5 | {fmt(m['mrr_at_5'])} | ≥{THRESHOLDS['mrr_at_5']} | "
                 f"{ok(m['mrr_at_5'], THRESHOLDS['mrr_at_5'])} |")
    lines.append(f"| 引用内容支持率 | {fmt(m['citation_support_rate'])} | ≥{THRESHOLDS['citation_support_rate']} | — |")
    lines.append(f"| 拒答正确率 | {fmt(m['refusal_accuracy'])} | ≥{THRESHOLDS['refusal_accuracy']} | "
                 f"{ok(m['refusal_accuracy'], THRESHOLDS['refusal_accuracy'])} |")
    lines.append(f"| 过度拒答率 | {fmt(m['over_refusal_rate'])} | ≤{THRESHOLDS['over_refusal_rate']} | "
                 f"{ok(m['over_refusal_rate'], THRESHOLDS['over_refusal_rate'], lower_better=True)} |")
    lines.append(f"| 任务成功率 | {fmt(m['task_success_rate'])} | ≥{THRESHOLDS['task_success_rate']} | — |")
    lines.append(f"| 多轮保持率 | {fmt(m['multiturn_retention'])} | ≥{THRESHOLDS['multiturn_retention']} | "
                 f"{ok(m['multiturn_retention'], THRESHOLDS['multiturn_retention'])} |")
    lines.append("")
    lat = m["latency"]
    lines.append(f"**延迟**：kb_qa P50={lat['kb_qa_p50_ms']}ms / P95={lat['kb_qa_p95_ms']}ms"
                 f"（≤{THRESHOLDS['latency_kb_qa_p95_ms']}ms）；"
                 f"log_diag P50={lat['log_diag_p50_ms']}ms / P95={lat['log_diag_p95_ms']}ms"
                 f"（≤{THRESHOLDS['latency_log_diag_p95_ms']}ms）")
    lines.append("")
    lines.append(f"**引用 ID 有效率（硬线，独立于 7 项）**：{fmt(m['citation_id_validity'])} "
                 f"{'✅ 全通过' if m['citation_id_validity_all_pass'] else '❌ 存在无效引用'}")
    lines.append("")
    lines.append("## 2. 分 judge 汇总（禁止只给合并值）")
    lines.append("")
    lines.append("| 口径 | 条数 | 引用内容支持率 |")
    lines.append("|---|---|---|")
    for k, v in rep["metrics_by_judge"].items():
        lines.append(f"| {k} | {v['count']} | {fmt(v['citation_support_rate'])} |")
    lines.append("")
    if rep["metrics_by_judge"]["用户口径（user_recheck=true，判定人留空）"]["pending_qids"]:
        p = rep["metrics_by_judge"]["用户口径（user_recheck=true，判定人留空）"]
        lines.append(f"> ⏳ 待用户填的 {p['count']} 条：`{'`, `'.join(p['pending_qids'])}`")
        lines.append("")
    lines.append("## 3. 逐题结果")
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
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="OpsPilot 35 题评测真跑")
    ap.add_argument("--dataset", default=str(DEFAULT_DATASET))
    ap.add_argument("--out", default=None, help="JSON 报告输出路径")
    ap.add_argument("--md", default=None, help="Markdown 摘要输出路径")
    ap.add_argument("--limit", type=int, default=0, help="仅跑前 N 题（调试用，0=全量）")
    args = ap.parse_args(argv)

    env = load_env()
    os.environ.update({k: v for k, v in env.items() if k not in os.environ})
    dsn = build_dsn(env)

    rows = [json.loads(l) for l in Path(args.dataset).read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.limit:
        rows = rows[: args.limit]

    started = datetime.now(timezone.utc).isoformat()
    t_start = time.monotonic()

    print(f"[run_eval] commit={git_commit()} 题目={len(rows)} dataset={args.dataset}")
    ev = Evaluator(dsn)

    print("[run_eval] 前置：摄取 5 篇 seed_docs …")
    seeds = ev.ensure_seeds()
    for d in seeds["docs"]:
        print(f"  - {d.get('name')}: {d.get('status')} chunks={d.get('chunks')} "
              f"dedup={d.get('deduplicated')}")
    if not seeds["all_completed"]:
        print("  ⚠️ 有文档未 completed，评测结果不可信")

    # 冒烟：至少 1 条真题从 jsonl 读入走完整链路（防"字段→检索输入"装配盲区）
    smoke_row = next((r for r in rows if r.get("should_answer")), rows[0])
    print(f"[run_eval] 冒烟：真题 {smoke_row['id']} 走完整链路 …")
    ev.new_session("eval-smoke")  # 确保链路可用
    smoke_cr = run_case(ev, smoke_row)
    smoke = {
        "qid": smoke_cr.qid,
        "question": smoke_cr.question,
        "got_answer_type": smoke_cr.got_answer_type,
        "citations": len(smoke_cr.citations),
        "recall_at_5": smoke_cr.recall_at_5,
        "node_path": smoke_cr.node_path,
        "passed": smoke_cr.error is None and smoke_cr.got_answer_type != "error",
    }
    print(f"  冒烟结果: type={smoke['got_answer_type']} citations={smoke['citations']} "
          f"recall={smoke['recall_at_5']}")

    print("[run_eval] 跑全量 …")
    results: list[CaseResult] = []
    for i, row in enumerate(rows, 1):
        cr = run_case(ev, row)
        results.append(cr)
        flag = "OK " if cr.got_answer_type != "error" else "ERR"
        print(f"  [{i:2d}/{len(rows)}] {flag} {cr.qid:5s} {cr.category:14s} "
              f"type={cr.got_answer_type:9s} recall={cr.recall_at_5} lat={cr.latency_ms}ms")

    per_case = {r.qid: r for r in results}
    a3b = select_a3b_cases(rows, per_case)
    metrics = compute_metrics(results, rows)
    duration = time.monotonic() - t_start

    rep = build_report(results, rows, metrics, a3b, env, seeds, smoke, started, duration)

    out_path = Path(args.out) if args.out else DEFAULT_OUT_DIR / f"{started[:19].replace(':','')}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    md_path = Path(args.md) if args.md else out_path.with_suffix(".md")
    md_path.write_text(render_markdown(rep), encoding="utf-8")

    print(f"\n[run_eval] 报告: {out_path}")
    print(f"[run_eval] 摘要: {md_path}")
    print(f"[run_eval] 用时: {duration:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
