"""evidence 节点：规则层引用校验 + checker LLM 兜底（agent-workflow §3.4）。

规则层（**必定执行，不调模型**）：
  每个 `citation.chunk_id` 必须存在且属于本次 `chunks` 集合；不在集合内 →
  移入 `invalid_citations` 并删除该引用。

LLM 兜底层：仅当规则判"存疑"且答案含强断言时调用（模型 LLM_MODEL_CHECKER，
temperature=0，max_tokens=256）。被预算抢占时 → `degrade_reasons += checker_llm_skipped`，
**退回纯规则结论**（不阻断）。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time

from app.graph.budget import PRIORITY_CHECKER, Budget

logger = logging.getLogger("app.graph.evidence")

# 强断言特征（触发 checker LLM 兜底）
STRONG_CLAIM_PATTERNS = (
    r"根因(是|为)",
    r"一定(是|会)",
    r"必然是",
    r"肯定是",
    r"绝对",
    r"唯一(原因|可能)",
    r"就是这么",
)


def has_strong_claim(text: str) -> bool:
    return any(re.search(p, text or "") for p in STRONG_CLAIM_PATTERNS)


def evidence_node(
    state: dict,
    *,
    budget: Budget,
    llm_call=None,
    record_step=None,
) -> dict:
    """evidence 节点实现。

    **B2 修正（关键）**：`kb_qa` 主路径本就没有 `draft_answer`（草稿由 `final` 生成），
    此时 evidence 只能拿到空 draft。早期实现在这种"无米下锅"的情况下输出
    `supported=False`，这个**假阴性**信号流进 final 的 prompt 后，模型会据此拒答 ——
    即"检索到好证据却拒答"的演示事故级缺陷。

    现改为：
      - `draft_answer` 为空 → `method="skip"`, `supported=None`（**不是 False**），
        并跳过 checker（无草稿无从校验）。
      - 引用裁剪照常执行（硬约束），结果通过 `citations` 传给 final。
    """
    t0 = time.monotonic()
    chunks = state.get("chunks") or []
    draft = state.get("draft_answer") or ""
    citations = list(state.get("citations") or [])

    valid_ids = {c["chunk_id"] for c in chunks}
    kept: list[dict] = []
    invalid: list[str] = []
    for cit in citations:
        if cit.get("chunk_id") in valid_ids:
            kept.append(cit)
        else:
            invalid.append(cit.get("chunk_id", ""))

    degraded = bool(state.get("degraded", False))
    reasons: list[str] = list(state.get("degrade_reasons") or [])

    # --- B2：无草稿 → skip 语义，不产生假阴性 ---
    if not draft:
        step = {
            "node": "evidence",
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "input_digest": _digest(""),
            "output_summary": (
                f"method=skip, supported=None, kept={len(kept)}, invalid={len(invalid)}"
            ),
            "degraded": degraded,
        }
        if record_step:
            record_step(step)
        return {
            "citations": kept,
            "check_result": {
                "supported": None,  # 关键：None ≠ False
                "invalid_citations": invalid,
                "unsupported_claims": [],
                "method": "skip",
            },
            "degraded": degraded,
            "degrade_reasons": reasons,
        }

    # --- 有草稿：规则层判定 ---
    supported = bool(kept) and not invalid
    method = "rule"

    # LLM 兜底层：规则判"存疑"（有引用但不完整/有强断言）时才调用
    suspect = (not supported) or has_strong_claim(draft)
    if suspect and llm_call is not None:
        if budget.try_acquire("checker", PRIORITY_CHECKER):
            try:
                llm_call("checker", f"判断以下结论是否被片段支持，只输出 supported/unsupported。\n{draft}")
                budget.consume_llm()
                method = "llm"
            except Exception as exc:  # noqa: BLE001
                logger.warning("checker LLM 失败，退回纯规则: %s", exc)
                if "checker_timeout" not in reasons:
                    reasons.append("checker_timeout")
        else:
            # 被抢占 → 退回纯规则
            if "checker_llm_skipped" not in reasons:
                reasons.append("checker_llm_skipped")

    check_result = {
        "supported": supported,
        "invalid_citations": invalid,
        "unsupported_claims": [],
        "method": method,
    }

    step = {
        "node": "evidence",
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "input_digest": _digest(draft),
        "output_summary": (
            f"supported={supported}, kept={len(kept)}, invalid={len(invalid)}, method={method}"
        ),
        "degraded": degraded,
    }
    if record_step:
        record_step(step)

    return {
        "citations": kept,
        "check_result": check_result,
        "degraded": degraded,
        "degrade_reasons": reasons,
    }


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]