"""`analyze_error_log`：分析用户粘贴的日志文本（tool-policy §2.1 / PRD FR-23）。

纯正则 + 规则匹配。**不调模型、不执行命令、不读文件**。
"""

from __future__ import annotations

import re
from typing import Any

from app.tools.base import ToolError

MAX_LOG_CHARS = 8192

# 错误类型判定（顺序即优先级）
ERROR_TYPE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("connection_refused", re.compile(r"connection refused|econnrefused|连接被拒绝", re.I)),
    ("connection_timeout", re.compile(r"connection timed out|etimedout|连接超时|i/o timeout", re.I)),
    ("oom_killed", re.compile(r"oomkilled|out of memory|exit code 137|内存不足", re.I)),
    ("dns_failure", re.compile(r"no such host|name resolution|servfail|nxdomain", re.I)),
    ("permission_denied", re.compile(r"permission denied|eacces|access denied", re.I)),
    ("disk_full", re.compile(r"no space left|disk full|enospc", re.I)),
    ("port_in_use", re.compile(r"address already in use|eaddrinuse|端口.{0,6}占用", re.I)),
    ("auth_failure", re.compile(r"unauthorized|authentication failed|access denied for user", re.I)),
    ("panic", re.compile(r"panic:|fatal error:", re.I)),
]

# 错误码（UNIX errno 名 / HTTP 状态 / MySQL 错误号 / 常见 E 码）
ERROR_CODE_RE = re.compile(
    r"\b(E[A-Z]{3,}|[A-Z]{2,}_[A-Z_]+|ERROR\s*\d{3,5}|\b[45]\d{2}\b)\b"
)
# 时间戳（ISO8601 / 常见日志格式）
TIMESTAMP_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?|\d{2}:\d{2}:\d{2})\b"
)
# ERROR 等关键行
KEY_LINE_RE = re.compile(r"\b(error|fatal|panic|exception|failed|critical)\b", re.I)
# 分组模式（提取重复出现的短语）
GROUP_PATTERN_RE = re.compile(r"\b([a-z_]{3,}\s+[a-z_]{3,})\b", re.I)

ALLOWED_LANGUAGES = ("auto", "zh", "en")


def analyze_error_log(args: dict[str, Any]) -> dict[str, Any]:
    """入参：`log_text`(1–8192)、可选 `language`。"""
    log_text = args.get("log_text")
    if not isinstance(log_text, str) or not log_text.strip():
        raise ToolError("INVALID_TOOL_ARGS", "log_text 必须为非空字符串，长度 1–8192")

    language = args.get("language", "auto")
    if language not in ALLOWED_LANGUAGES:
        raise ToolError("INVALID_TOOL_ARGS", f"language 必须是 {ALLOWED_LANGUAGES} 之一")

    truncated_input = False
    if len(log_text) > MAX_LOG_CHARS:
        log_text = log_text[:MAX_LOG_CHARS]
        truncated_input = True

    lines = log_text.splitlines()

    error_type = "unknown"
    for name, pattern in ERROR_TYPE_RULES:
        if pattern.search(log_text):
            error_type = name
            break

    key_lines = [i + 1 for i, ln in enumerate(lines) if KEY_LINE_RE.search(ln)]
    timestamps = TIMESTAMP_RE.findall(log_text)
    error_codes = list(dict.fromkeys(ERROR_CODE_RE.findall(log_text)))

    counts: dict[str, int] = {}
    for m in GROUP_PATTERN_RE.finditer(log_text):
        phrase = m.group(1).lower()
        counts[phrase] = counts.get(phrase, 0) + 1
    groups = [
        {"pattern": p, "count": c}
        for p, c in sorted(counts.items(), key=lambda kv: -kv[1])
        if c >= 2
    ][:20]

    # 不做：不解释根因、不给结论（结论归 troubleshoot 节点）
    return {
        "error_type": error_type,
        "key_lines": key_lines[:50],
        "timestamps": timestamps[:20],
        "error_codes": error_codes[:20],
        "groups": groups,
        "truncated": truncated_input,
        "line_count": len(lines),
    }