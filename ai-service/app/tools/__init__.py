"""工具层入口：封闭白名单 + 统一执行器（tool-policy §1/§4/§5）。

**TOOL_REGISTRY 是常量**：不允许运行时注册、不允许插件式扩展、不允许从配置读工具名。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from app.tools.analyze_error_log import analyze_error_log
from app.tools.base import (
    MAX_TOOL_CALLS_PER_TURN,
    ToolError,
    ToolNotAllowedError,
    args_digest,
    canonical_json,
    run_tool,
)
from app.tools.check_port_conflict import check_port_conflict
from app.tools.search_knowledge_base import search_knowledge_base

logger = logging.getLogger("app.tools")

# §1 封闭白名单 —— 常量，只读
TOOL_REGISTRY: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "analyze_error_log": analyze_error_log,
    "check_port_conflict": check_port_conflict,
    "search_knowledge_base": search_knowledge_base,
}
assert len(TOOL_REGISTRY) == 3, "V1 白名单封闭为 3 个工具"


class ToolExecutor:
    """单轮工具执行器：白名单校验 + 次数限制 + 同参数去重（P5）+ 审计。"""

    def __init__(self, max_calls: int = MAX_TOOL_CALLS_PER_TURN) -> None:
        self.max_calls = max_calls
        self.calls: list[dict[str, Any]] = []          # 审计行（写 tool_calls 表）
        self._cache: dict[str, dict[str, Any]] = {}    # P5：同参数复用结果

    @property
    def count(self) -> int:
        return len([c for c in self.calls if c["status"] != "rejected"])

    def invoke(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """执行工具。**不抛业务异常**（除白名单拒绝按 §1 语义上抛给调用方）。

        返回 `{"status", "result", "tool_name", "args_digest", "duration_ms", "degraded"}`。
        """
        # §1：非白名单 → 400 TOOL_NOT_ALLOWED（记审计，不抛 500）
        if name not in TOOL_REGISTRY:
            record = {
                "tool_name": name,
                "args_digest": args_digest(args),
                "status": "rejected",
                "duration_ms": 0,
            }
            self.calls.append(record)
            logger.warning("拒绝非白名单工具: %s", name)
            raise ToolNotAllowedError(name)

        # §4：单轮次数限制 —— 第 4 次起直接跳过，degraded=true
        if self.count >= self.max_calls:
            record = {
                "tool_name": name,
                "args_digest": args_digest(args),
                "status": "rejected",
                "duration_ms": 0,
            }
            self.calls.append(record)
            logger.info("工具调用次数超限（%d），跳过 %s", self.max_calls, name)
            return {
                "status": "skipped",
                "result": {"error": {"code": "TOOL_BUDGET_EXCEEDED", "message": "超出单轮工具调用次数"}},
                "tool_name": name,
                "args_digest": record["args_digest"],
                "duration_ms": 0,
                "degraded": True,
            }

        # P5：同参数复用缓存（规范化 JSON 保证同参数得同键）
        cache_key = f"{name}:{canonical_json(args)}"
        if cache_key in self._cache:
            logger.info("工具 %s 同参数命中缓存，复用结果", name)
            return self._cache[cache_key]

        result, record = run_tool(name, args, TOOL_REGISTRY[name])
        self.calls.append(record)
        outcome = {
            "status": record["status"],
            "result": result,
            "tool_name": name,
            "args_digest": record["args_digest"],
            "duration_ms": record["duration_ms"],
            "degraded": record["status"] in ("timeout", "error", "rejected"),
        }
        self._cache[cache_key] = outcome
        return outcome

    def invoke_safe(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """宽松版本：白名单拒绝也转为结构化结果（供节点内使用，不中断本轮）。"""
        try:
            return self.invoke(name, args)
        except ToolNotAllowedError as exc:
            return {
                "status": "rejected",
                "result": {"error": {"code": exc.code, "message": exc.message}},
                "tool_name": name,
                "args_digest": "",
                "duration_ms": 0,
                "degraded": True,
            }


def get_tool(name: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """按白名单取工具；不在白名单 → ToolNotAllowedError。"""
    if name not in TOOL_REGISTRY:
        raise ToolNotAllowedError(name)
    return TOOL_REGISTRY[name]


__all__ = [
    "TOOL_REGISTRY",
    "ToolExecutor",
    "ToolError",
    "ToolNotAllowedError",
    "get_tool",
]