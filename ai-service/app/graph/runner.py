"""LangGraph 全图组装与 run_turn 单入口（agent-workflow §1 / §8）。

图结构（5 业务节点 + 2 确定性终止节点）：
    START → router ─ smalltalk/out_of_scope → smalltalk → END
                   └ kb_qa/troubleshooting → retrieval
                        ├ (无 chunks 或 top_score < min_score) → refuse → END
                        ├ kb_qa           → evidence → final → END
                        └ troubleshooting → troubleshoot → evidence → final → END

**唯一入口** `run_turn(req, emit=None)`：emit=None 非流式；emit 非空则在关键点回调
事件（S7 流式接入）。两条路径共用同一图，**不允许存在第二套编排代码**。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable

from langgraph.graph import END, StateGraph

from app.chat import ChatError
from app.config import get_config
from app.graph.budget import Budget, BudgetExhausted, DeadlineExceeded, MAX_STEPS
from app.graph.evidence import evidence_node
from app.graph.final import final_node, refuse_node, smalltalk_node
from app.graph.retrieval import retrieval_node
from app.graph.router import router_node
from app.graph.state import State
from app.graph.troubleshoot import has_log_signal, troubleshoot_node
from app.llm import make_llm_call
from app.tools import ToolExecutor

logger = logging.getLogger("app.graph")

Event = dict[str, Any]


# ---------------------------------------------------------------------------
# 条件边
# ---------------------------------------------------------------------------


def route_after_router(state: State) -> str:
    return {
        "kb_qa": "retrieval",
        "troubleshooting": "retrieval",
        "smalltalk": "smalltalk",
        "out_of_scope": "smalltalk",
    }[state["route"]]


def make_route_after_retrieval(cfg: dict) -> Callable[[State], str]:
    min_score = float(cfg["retrieval_min_score"])

    def route_after_retrieval(state: State) -> str:
        chunks = state.get("chunks") or []
        # refuse 判定用**最高分**而非平均分（agent-workflow §4 注释）
        if not chunks or float(chunks[0]["score"]) < min_score:
            return "refuse"
        if state.get("route") == "troubleshooting" and has_log_signal(state):
            return "troubleshoot"
        return "evidence"

    return route_after_retrieval


def route_after_evidence(state: State) -> str:
    return "final"  # 恒进入 final；证据不足只影响 citations 与 answer_type


# ---------------------------------------------------------------------------
# 图构建
# ---------------------------------------------------------------------------


class Runner:
    """封装一轮的执行上下文（预算、步骤记录、llm 调用）。"""

    def __init__(
        self,
        cfg: dict,
        llm_call=None,
        tool_executor=None,
        retrieve_fn=None,
    ) -> None:
        self.cfg = cfg
        self.budget = Budget()
        self.steps: list[dict] = []
        self.llm_call = llm_call
        self.tool_executor = tool_executor
        self.retrieve_fn = retrieve_fn

    def record_step(self, step: dict) -> None:
        self.steps.append(step)

    # --- 节点包装：注入 budget / 记录轨迹 ---

    def _n_router(self, state: State) -> dict:
        self.budget.new_step("router")
        out = router_node(
            state, budget=self.budget, llm_call=self.llm_call, record_step=self.record_step
        )
        out.pop("_router_llm_calls", None)
        return out

    def _n_retrieval(self, state: State) -> dict:
        self.budget.new_step("retrieval")
        return retrieval_node(
            state,
            cfg=self.cfg,
            budget=self.budget,
            llm_call=self.llm_call,
            record_step=self.record_step,
            retrieve_fn=self.retrieve_fn,
        )

    def _n_troubleshoot(self, state: State) -> dict:
        self.budget.new_step("troubleshoot")
        return troubleshoot_node(
            state,
            cfg=self.cfg,
            budget=self.budget,
            llm_call=self.llm_call,
            tool_executor=self.tool_executor,
            record_step=self.record_step,
        )

    def _n_evidence(self, state: State) -> dict:
        self.budget.new_step("evidence")
        return evidence_node(
            state, budget=self.budget, llm_call=self.llm_call, record_step=self.record_step
        )

    def _n_final(self, state: State) -> dict:
        self.budget.new_step("final")
        return final_node(
            state,
            cfg=self.cfg,
            budget=self.budget,
            llm_call=self.llm_call,
            record_step=self.record_step,
        )

    def _n_refuse(self, state: State) -> dict:
        self.budget.new_step("refuse")
        return refuse_node(state, record_step=self.record_step)

    def _n_smalltalk(self, state: State) -> dict:
        self.budget.new_step("smalltalk")
        return smalltalk_node(state, record_step=self.record_step)

    def build(self):
        g = StateGraph(State)
        g.add_node("router", self._n_router)
        g.add_node("retrieval", self._n_retrieval)
        g.add_node("troubleshoot", self._n_troubleshoot)
        g.add_node("evidence", self._n_evidence)
        g.add_node("final", self._n_final)
        g.add_node("refuse", self._n_refuse)
        g.add_node("smalltalk", self._n_smalltalk)

        g.set_entry_point("router")
        g.add_conditional_edges(
            "router",
            route_after_router,
            {"retrieval": "retrieval", "smalltalk": "smalltalk"},
        )
        g.add_conditional_edges(
            "retrieval",
            make_route_after_retrieval(self.cfg),
            {"refuse": "refuse", "troubleshoot": "troubleshoot", "evidence": "evidence"},
        )
        g.add_edge("troubleshoot", "evidence")
        g.add_conditional_edges("evidence", route_after_evidence, {"final": "final"})
        g.add_edge("final", END)
        g.add_edge("refuse", END)
        g.add_edge("smalltalk", END)
        return g.compile()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def run_turn(
    payload: dict[str, Any],
    emit: Callable[[Event], None] | None = None,
    *,
    llm_call=None,
    tool_executor=None,
    retrieve_fn=None,
) -> dict[str, Any]:
    """执行一轮问答（全图）。

    入参对齐 TurnRequest（含内部扩展 owner_id）；出参对齐 TurnResult。
    `emit` 非空时在关键点回调事件（meta/citation/done/error），供 S7 流式复用。
    """
    cfg = get_config()
    started = time.monotonic()

    request_id = str(payload.get("request_id") or uuid.uuid4())
    session_id = str(payload.get("session_id") or "")
    turn_index = int(payload.get("turn_index", 0))
    question = str(payload.get("question") or "").strip()
    owner_id = str(payload.get("owner_id") or "")
    history = payload.get("history") or []
    attach_log = payload.get("attach_log")

    if not question:
        raise ChatError("INVALID_QUESTION", "问题为空")
    if not owner_id:
        raise ChatError("INVALID_REQUEST", "缺少 owner_id")

    run_id = str(uuid.uuid4())
    if emit:
        emit({"type": "meta", "run_id": run_id, "session_id": session_id, "request_id": request_id})

    runner = Runner(
        cfg,
        llm_call=llm_call or make_llm_call(cfg),
        # S7：默认注入真工具执行器（封闭白名单 + 次数限制 + 去重 + 审计）。
        # 测试可传自己的 executor 覆盖。
        tool_executor=tool_executor or ToolExecutor(),
        retrieve_fn=retrieve_fn,
    )
    app = runner.build()

    init: State = {
        "request_id": request_id,
        "session_id": session_id,
        "turn_index": turn_index,
        "question": question,
        "history": history,
        "attach_log": attach_log,
        "owner_id": owner_id,
        "degraded": False,
        "degrade_reasons": [],
        "preempted": [],
        "steps": [],
        "usage": {"llm_calls": 0, "tool_calls": 0, "prompt_tokens": 0, "completion_tokens": 0},
        "terminated_reason": "completed",
    }

    terminated_reason = "completed"
    try:
        final_state = app.invoke(init)
    except DeadlineExceeded:
        terminated_reason = "deadline_exceeded"
        final_state = dict(init)
    except BudgetExhausted as exc:
        terminated_reason = "budget_exhausted" if "MAX_STEPS" not in str(exc) else "step_limit"
        final_state = dict(init)
    except Exception as exc:  # noqa: BLE001
        logger.exception("图执行失败: %s", exc)
        terminated_reason = "model_unavailable"
        final_state = dict(init)

    # 未走到终止节点的兜底（预算/超时中断）
    if not final_state.get("final_text"):
        final_state["final_text"] = "未能在预算内完成回答，请稍后重试。"
        final_state["answer_type"] = "degraded"
        final_state["degraded"] = True
        reasons = list(final_state.get("degrade_reasons") or [])
        if terminated_reason not in reasons:
            reasons.append(terminated_reason)
        final_state["degrade_reasons"] = reasons

    citations = final_state.get("citations") or []
    answer_type = final_state.get("answer_type") or "degraded"
    degraded = bool(final_state.get("degraded", False))
    reasons = list(final_state.get("degrade_reasons") or [])
    preempted = list(runner.budget.preempted)

    if emit:
        emit({"type": "citation", "citations": citations})
        emit(
            {
                "type": "done",
                "usage": {"llm_calls": runner.budget.llm_calls, "tool_calls": runner.budget.tool_calls},
            }
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    node_path = [s["node"] for s in runner.steps]

    return {
        "run_id": run_id,
        "session_id": session_id,
        "route": final_state.get("route") or "kb_qa",
        "final_text": final_state.get("final_text") or "",
        "answer_type": answer_type,
        "citations": citations,
        "degraded": degraded,
        "degrade_reasons": reasons,
        "preempted": preempted,
        "terminated_reason": terminated_reason,
        "usage": {
            "llm_calls": runner.budget.llm_calls,
            "tool_calls": runner.budget.tool_calls,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        },
        "duration_ms": duration_ms,
        # --- 轨迹（Go 落 run_steps / agent_runs 用）---
        "node_path": node_path,
        "steps": runner.steps,
        # S7：工具审计行（Go 落 tool_calls 表）。只含摘要，不含原始入参（NFR-S5）。
        "tool_call_records": (
            runner.tool_executor.calls if hasattr(runner.tool_executor, "calls") else []
        ),
        "retrieval_top_score": (
            (final_state.get("chunks") or [{}])[0].get("score", 0.0)
            if final_state.get("chunks")
            else 0.0
        ),
        "retrieved_count": len(final_state.get("chunks") or []),
        "rewritten_query": final_state.get("rewritten_query"),
        # 内部字段（评测/验收用，计算 Recall@5 需要完整 Top-K 集合，而非仅被引用的切片）。
        # Go 侧 TurnResult 未声明该字段，json 解码时会被忽略，不影响契约。
        "retrieved_chunk_ids": [c.get("chunk_id") for c in (final_state.get("chunks") or [])],
    }


__all__ = ["run_turn", "Runner", "MAX_STEPS"]