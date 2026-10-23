"""工具层的公共基础设施：参数校验、超时、输出截断、审计摘要。

对齐 `docs/architecture/tool-policy.md`：
  §3 硬性禁令（无通用执行器）
  §4 执行限制（单工具 5s / 单轮 ≤3 / 输出 ≤4KB）
  §5 审计（只存摘要 + canonical JSON 的 sha256）
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Callable

logger = logging.getLogger("app.tools")

# §4 执行限制
TOOL_TIMEOUT_SECONDS = 5.0
MAX_TOOL_CALLS_PER_TURN = 3
MAX_OUTPUT_BYTES = 4096  # 4KB

# §5：args_digest = sha256(canonical_json(args))[:32]
_DIGEST_LEN = 32


class ToolError(Exception):
    """工具层错误，映射为结构化状态。"""

    def __init__(self, code: str, message: str, *, http_status: int = 422) -> None:
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)


class ToolNotAllowedError(ToolError):
    """非白名单工具 → 400 TOOL_NOT_ALLOWED（§1）。"""

    def __init__(self, name: str) -> None:
        super().__init__(
            "TOOL_NOT_ALLOWED",
            f"工具 {name!r} 不在白名单内",
            http_status=400,
        )


def canonical_json(obj: Any) -> str:
    """规范化 JSON（键排序、无多余空白），保证同参数得同摘要（P5 去重前提）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def args_digest(args: dict[str, Any]) -> str:
    """§5：只存摘要，不存原始入参（日志可能含敏感信息）。"""
    digest = hashlib.sha256(canonical_json(args).encode("utf-8")).hexdigest()
    return f"sha256:{digest[:_DIGEST_LEN]}"


def truncate_output(result: dict[str, Any], limit: int = MAX_OUTPUT_BYTES) -> dict[str, Any]:
    """§4：单次输出 ≤4KB，超限截断并标 `truncated=true`。"""
    payload = canonical_json(result)
    if len(payload.encode("utf-8")) <= limit:
        result.setdefault("truncated", False)
        return result

    # 截断策略：优先保留结构，把最占空间的列表字段逐项裁剪
    trimmed = dict(result)
    trimmed["truncated"] = True
    for key in ("key_lines", "timestamps", "error_codes", "groups", "chunks", "conflicts"):
        if key in trimmed and isinstance(trimmed[key], list):
            items = list(trimmed[key])
            while items and len(canonical_json({**trimmed, key: items}).encode("utf-8")) > limit:
                items.pop()
            trimmed[key] = items
    return trimmed


def run_tool(
    name: str,
    args: dict[str, Any],
    impl: Callable[[dict[str, Any]], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """统一执行入口：计时 + 异常归一到 status + 输出截断。

    返回 `(result, record)`；record 用于写 `tool_calls` 审计行（§5）。
    注意：**不抛异常**（除非调用方需要），业务失败通过 record.status 表达。
    """
    started = time.monotonic()
    status = "ok"
    result: dict[str, Any] = {}
    try:
        result = impl(args)
    except ToolNotAllowedError:
        raise
    except ToolError as exc:
        status = "rejected"
        result = {"error": {"code": exc.code, "message": exc.message}}
    except Exception as exc:  # noqa: BLE001
        logger.warning("工具 %s 执行异常: %s", name, exc)
        status = "error"
        result = {"error": {"code": "TOOL_ERROR", "message": str(exc)}}

    duration_ms = int((time.monotonic() - started) * 1000)
    if duration_ms > TOOL_TIMEOUT_SECONDS * 1000 and status == "ok":
        status = "timeout"

    if status == "ok":
        result = truncate_output(result)

    record = {
        "tool_name": name,
        "args_digest": args_digest(args),
        "status": status,
        "duration_ms": duration_ms,
    }
    return result, record