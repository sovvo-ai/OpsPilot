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

# 端口线索：出现"端口/port"或形如 8080 的数字 + 冲突/占用类词
PORT_HINT = re.compile(
    r"(端口|port|占用|冲突|被占|already in use|起不来|address)"
    r"|(\b\d{2,5}\b.{0,12}(起不来|不通|占用|冲突|监听))",
    re.IGNORECASE,
)

_PORT_RE = re.compile(r"\b(\d{2,5})\b")


def _extract_ports(text: str) -> list[dict[str, Any]]:
    """从问句里提取端口，构造 check_port_conflict 的入参。

    这是**启发式提取**：V1 只分析用户提供的模拟数据，不探测主机。
    提取不到合法端口时给一个占位（工具会按范围校验）。
    """
    ports: list[dict[str, Any]] = []
    seen: set[int] = set()
    for m in _PORT_RE.finditer(text):
        p = int(m.group(1))
        if 1 <= p <= 65535 and p not in seen:
            seen.add(p)
            ports.append({"port": p, "protocol": "tcp"})
        if len(ports) >= 20:
            break
    return ports or [{"port": 8080, "protocol": "tcp"}]


def _as_record(out: dict[str, Any]) -> dict[str, Any]:
    """把 executor 输出转成 tool_calls 审计行（写库用）。"""
    return {
        "tool_name": out.get("tool_name", ""),
        "args_digest": out.get("args_digest", ""),
        "status": out.get("status", "error"),
        "duration_ms": out.get("duration_ms", 0),
        "output_summary": _summarize_tool(out),
    }


def _summarize_tool(out: dict[str, Any]) -> str:
    """工具输出摘要（≤200 字，供 trace 展示，不落原文）。"""
    res = out.get("result") or {}
    if out.get("status") != "ok":
        err = (res.get("error") or {}).get("code", "?")
        return f"status={out.get('status')}, error={err}"
    if "error_type" in res:
        return f"error_type={res['error_type']}, codes={res.get('error_codes')}"
    if "conflicts" in res:
        return f"conflicts={len(res['conflicts'])}, checked={res.get('checked')}"
    if "chunks" in res:
        return f"chunks={res.get('count')}"
    return "ok"


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
    """troubleshoot 节点实现（S7：接真工具）。

    工具调用链：
      1. `analyze_error_log`（有 attach_log 或日志特征时）
      2. `check_port_conflict`（问句含端口+冲突线索时）
    两者的输出作为**工具证据**注入生成 prompt（FR-18②：工具结论需标注证据来源，
    不要求 chunk 引用）。
    """
    t0 = time.monotonic()
    degraded = bool(state.get("degraded", False))
    reasons: list[str] = list(state.get("degrade_reasons") or [])
    tool_calls: list[dict] = list(state.get("tool_calls") or [])
    chunks = state.get("chunks") or []
    evidence_blocks: list[str] = []      # 工具证据（供 prompt）

    # --- 工具调用（CP3：额度不足则跳过；executor 内部再限 3 次）---
    if tool_executor is not None:
        attach_log = state.get("attach_log") or ""
        question = state.get("question", "")

        # 工具 1：analyze_error_log
        if attach_log or LOG_HINT.search(question):
            if budget.try_acquire_tool():
                out = tool_executor.invoke_safe("analyze_error_log", {"log_text": attach_log or question})
                budget.consume_tool()
                tool_calls.append(_as_record(out))
                if out["status"] == "ok":
                    r = out["result"]
                    evidence_blocks.append(
                        f"[工具 analyze_error_log] error_type={r.get('error_type')}, "
                        f"error_codes={r.get('error_codes')}, 关键行={r.get('key_lines')}"
                    )
                else:
                    degraded = True
                    if "tool_failed" not in reasons:
                        reasons.append("tool_failed")
            else:
                degraded = True
                if "tool_skipped_budget" not in reasons:
                    reasons.append("tool_skipped_budget")

        # 工具 2：check_port_conflict（问句含端口线索）
        if PORT_HINT.search(question):
            if budget.try_acquire_tool():
                out = tool_executor.invoke_safe(
                    "check_port_conflict",
                    {"ports": _extract_ports(question), "expected_owner": None},
                )
                budget.consume_tool()
                tool_calls.append(_as_record(out))
                if out["status"] == "ok":
                    evidence_blocks.append(
                        f"[工具 check_port_conflict] {out['result'].get('summary')}"
                    )
                elif out["status"] != "skipped":
                    degraded = True
                    if "tool_failed" not in reasons:
                        reasons.append("tool_failed")

    # 第 1 次 LLM（优先级高）
    draft = None
    prompt = build_prompt(state.get("question", ""), chunks, state.get("history") or [])
    if evidence_blocks:
        prompt += (
            "\n\n【工具证据】（以下为工具对日志/端口的分析结果，"
            "基于它的结论请标注来源为『工具』，不需要 chunk 引用）\n"
            + "\n".join(evidence_blocks)
        )
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