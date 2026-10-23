"""troubleshoot 节点：LLM + 工具（agent-workflow §3.3）。

前置：`route=troubleshooting` 且输入含日志特征。无日志线索时节点被跳过
（静态边直连 evidence）。

工具额度不足（CP3）→ 跳过并 `degraded=true`、`degrade_reasons += tool_skipped_budget`，
**不抛错**（工具非必需）。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time

from app.chat import build_prompt
from app.graph.budget import PRIORITY_HIGH, Budget

logger = logging.getLogger("app.graph.troubleshoot")

LOG_HINT = re.compile(
    r"(traceback|exception|error|failed|panic|\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}|"
    r"日志|报错|堆栈|at [\w.$]+\(|\b[45]\d{2}\b)",
    re.IGNORECASE,
)


def has_log_signal(state: dict) -> bool:
    """是否含日志/错误线索（决定 troubleshoot 是否执行）。"""
    if state.get("attach_log"):
        return True
    q = state.get("question", "")
    return bool(LOG_HINT.search(q))


def troubleshoot_node(
    state: dict,
    *,
    cfg: dict,
    budget: Budget,
    llm_call=None,
    tool_executor=None,
    record_step=None,
) -> dict:
    """troubleshoot 节点实现。"""
    t0 = time.monotonic()
    degraded = bool(state.get("degraded", False))
    reasons: list[str] = list(state.get("degrade_reasons") or [])
    tool_calls: list[dict] = list(state.get("tool_calls") or [])
    chunks = state.get("chunks") or []

    # 工具调用（CP3：额度不足则跳过）
    if tool_executor is not None and state.get("attach_log"):
        if budget.try_acquire_tool():
            try:
                rec = tool_executor("analyze_error_log", state["attach_log"])
                budget.consume_tool()
                tool_calls.append(rec)
            except Exception as exc:  # noqa: BLE001
                logger.warning("工具调用失败: %s", exc)
                degraded = True
                if "tool_failed" not in reasons:
                    reasons.append("tool_failed")
        else:
            degraded = True
            if "tool_skipped_budget" not in reasons:
                reasons.append("tool_skipped_budget")

    # 第 1 次 LLM（优先级高）
    draft = None
    prompt = build_prompt(state.get("question", ""), chunks, state.get("history") or [])
    if llm_call is not None and budget.try_acquire("troubleshoot", PRIORITY_HIGH):
        try:
            draft = llm_call("troubleshoot", prompt)
            budget.consume_llm()
        except Exception as exc:  # noqa: BLE001
            logger.warning("troubleshoot 生成失败: %s", exc)
            degraded = True
            if "troubleshoot_timeout" not in reasons:
                reasons.append("troubleshoot_timeout")

    step = {
        "node": "troubleshoot",
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "input_digest": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
        "output_summary": f"draft_len={len(draft or '')}, tool_calls={len(tool_calls)}",
        "degraded": degraded,
    }
    if record_step:
        record_step(step)

    return {
        "draft_answer": draft,
        "tool_calls": tool_calls,
        "degraded": degraded,
        "degrade_reasons": reasons,
    }