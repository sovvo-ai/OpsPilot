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
_PID_RE = re.compile(r"\b(?:pid|PID|进程号|进程)\s*[:=]?\s*(\d{1,7})\b")
_PROTO_RE = re.compile(r"\b(tcp|udp)\b", re.IGNORECASE)
# 服务名：端口附近的标识符（字母开头、长度≥2，排除常见噪声词）
_SERVICE_RE = re.compile(r"\b([a-zA-Z][a-zA-Z0-9_.-]{1,30})\b")


def _extract_ports(text: str) -> list[dict[str, Any]]:
    """从问句**逐行**提取端口占用信息，供 check_port_conflict 使用。

    **S7-B 修正（关键）**：早期实现用 `seen: set[int]` 对端口去重，把
    "8080 nginx" 与 "8080 java" 两行压成一条 `{8080,tcp}` → 工具按"无重复端口"
    判定**无冲突**。而"两行同端口"恰恰是冲突的**定义来源** —— 这不是简化，
    是给出**错误结论**（比不调用更糟）。

    现改为逐行提取，同端口多行**保留多条**，并解析 `service` / `pid` / `protocol`。
    """
    ports: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue

        # 先摘出 pid，并从待扫描文本里挖掉 —— 避免把 `pid=1234` 的 1234 当成端口
        line_pid: int | None = None
        dm = _PID_RE.search(line)
        if dm:
            line_pid = int(dm.group(1))
        scan_text = _PID_RE.sub(" ", line)

        # service 就近归属：把行按端口切段，每个端口只在其**后随片段**里找 service
        # （"nginx 监听 8080" 的 nginx 在端口前，"8080 nginx" 在后，两者都要能取到）
        port_matches = list(_PORT_RE.finditer(scan_text))
        for mi, m in enumerate(port_matches):
            p = int(m.group(1))
            if not (1 <= p <= 65535):
                continue
            item: dict[str, Any] = {"port": p, "protocol": "tcp"}

            # protocol：行内显式声明优先
            pm = _PROTO_RE.search(line)
            if pm:
                item["protocol"] = pm.group(1).lower()

            if line_pid is not None:
                item["pid"] = line_pid

            # service 就近：优先取本端口后随片段，其次取前导片段
            after_start = m.end()
            after_end = port_matches[mi + 1].start() if mi + 1 < len(port_matches) else len(scan_text)
            before_start = port_matches[mi - 1].end() if mi > 0 else 0
            candidates = [scan_text[after_start:after_end], scan_text[before_start:m.start()]]
            for seg in candidates:
                seg = _PROTO_RE.sub(" ", seg)
                found = False
                for sm in _SERVICE_RE.finditer(seg):
                    word = sm.group(1)
                    if _is_noise_word(word):
                        continue
                    item["service"] = word
                    found = True
                    break
                if found:
                    break

            ports.append(item)
            if len(ports) >= 20:
                return ports

    # 兜底：整段文本里一个合法端口都没有时，给占位（工具会按范围校验）
    return ports or [{"port": 8080, "protocol": "tcp"}]


# 噪声词：这些词出现在端口附近时不应被当作 service 名
_NOISE_WORDS = {
    "port", "ports", "tcp", "udp", "pid", "listen", "listening", "used", "use",
    "already", "in", "the", "and", "or", "address", "conflict", "occupied",
    "端口", "占用", "冲突", "监听", "进程", "服务", "起不来", "不通",
}


def _is_noise_word(word: str) -> bool:
    return word.lower() in _NOISE_WORDS or len(word) < 2


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
    """是否含**需要进入 troubleshoot 节点**的线索。

    **S7-B 连带修正**：早期只认日志特征（`LOG_HINT`），导致"端口冲突"这类
    纯端口问句虽被判 troubleshooting，却在条件边被拦下、直连 evidence ——
    `check_port_conflict` 永远不会被调用（工具白装了）。
    现把端口线索（`PORT_HINT`）也计入。
    """
    if state.get("attach_log"):
        return True
    q = state.get("question", "")
    return bool(LOG_HINT.search(q) or PORT_HINT.search(q))


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