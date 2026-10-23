"""预算、抢占与 reserved_for_final（agent-workflow §5）。

三个检查点：
  CP1 入口    初始化 usage、锁定 RESERVED_FOR_FINAL、记录 deadline
  CP2 LLM 前  已用 ≥ MAX_LLM_CALLS → BudgetExhausted；now > deadline → DeadlineExceeded；
              节点需抢占时按 §5.3 顺序判定
  CP3 工具前  tool_calls 已达 MAX_TOOL_CALLS → 跳过并 degraded（不抛错）

抢占顺序（额度不足时）：
  final(1, 锁定不可抢) > {router, troubleshoot 第1次}（同档并列） > troubleshoot 第2次
  > checker LLM > rewrite
"""

from __future__ import annotations

import time
from typing import Any

# 常量（PRD FR-36 / 计划书 §6.3）
MAX_STEPS = 8
MAX_LLM_CALLS = 6
RESERVED_FOR_FINAL = 1
MAX_TOOL_CALLS = 3
WALL_CLOCK_MS = 60_000

# 节点优先级：数字越小越优先（final 恒成功，见 try_acquire）
PRIORITY_FINAL = 0
PRIORITY_HIGH = 1  # router、troubleshoot 第 1 次
PRIORITY_MID = 2   # troubleshoot 第 2 次
PRIORITY_CHECKER = 3
PRIORITY_REWRITE = 4


class BudgetExhausted(Exception):
    """LLM 额度耗尽。"""


class DeadlineExceeded(Exception):
    """墙钟超时。"""


class Budget:
    """一轮问答的预算与抢占控制器。"""

    def __init__(self, wall_clock_ms: int = WALL_CLOCK_MS) -> None:
        self.started_at = time.monotonic()
        self.deadline = self.started_at + wall_clock_ms / 1000.0
        self.llm_calls = 0
        self.tool_calls = 0
        self.max_llm_calls = MAX_LLM_CALLS
        self.max_tool_calls = MAX_TOOL_CALLS
        self.reserved = RESERVED_FOR_FINAL  # CP1：为 final 预留 1
        self.preempted: list[str] = []
        self.steps = 0

    # --- 时间 ---

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    def check_deadline(self) -> None:
        """CP2 的时间判定。"""
        if time.monotonic() > self.deadline:
            raise DeadlineExceeded("超过墙钟预算")

    # --- 步数 ---

    def new_step(self, node: str) -> None:
        """每进入一个节点计一步；超过 MAX_STEPS 抛错。"""
        self.steps += 1
        if self.steps > MAX_STEPS:
            raise BudgetExhausted(f"超过 MAX_STEPS={MAX_STEPS}")

    # --- LLM 额度 ---

    def available_llm(self) -> int:
        """可用额度 = 上限 - 已用 - 给 final 预留的份额。"""
        return max(0, self.max_llm_calls - self.llm_calls - self.reserved)

    def try_acquire(self, node: str, priority: int = PRIORITY_HIGH, is_final: bool = False) -> bool:
        """尝试获取一次 LLM 调用额度。

        final 恒成功（预留份额专供）；其余节点按剩余额度判定，失败即记 preempted。
        """
        self.check_deadline()
        if is_final:
            # final 可动用预留份额，但仍受总上限约束
            if self.llm_calls >= self.max_llm_calls:
                raise BudgetExhausted("final 也无法获取额度")
            return True
        if self.available_llm() > 0:
            return True
        # 额度不足：记抢占，返回 False，由节点走"无模型分支"
        if node not in self.preempted:
            self.preempted.append(node)
        return False

    def consume_llm(self) -> None:
        """实际发生一次 LLM 调用后记账。"""
        self.llm_calls += 1

    # --- 工具额度（CP3）---

    def try_acquire_tool(self) -> bool:
        """工具额度不足时返回 False（不抛错，工具非必需）。"""
        if self.tool_calls >= self.max_tool_calls:
            return False
        return True

    def consume_tool(self) -> None:
        self.tool_calls += 1

    # --- 快照 ---

    def usage(self) -> dict[str, int]:
        return {"llm_calls": self.llm_calls, "tool_calls": self.tool_calls}

    def snapshot(self) -> dict[str, Any]:
        return {
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "steps": self.steps,
            "preempted": list(self.preempted),
            "elapsed_ms": self.elapsed_ms(),
        }