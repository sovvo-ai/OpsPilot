"""LangGraph State schema（agent-workflow §2）。

字段与文档逐项对齐；`total=False` 便于节点只返回增量。
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


class RetrievedChunk(TypedDict, total=False):
    chunk_id: str
    doc_id: str
    doc_title: str
    score: float
    content: str
    heading_path: str


class ToolCallRecord(TypedDict, total=False):
    tool_call_id: str
    tool_name: str
    status: str
    args_digest: str
    duration_ms: int
    output_summary: str


class Citation(TypedDict, total=False):
    chunk_id: str
    doc_id: str
    doc_title: str
    snippet: str
    score: float
    source_type: str
    tool_call_id: str
    status: str


class HistoryMessage(TypedDict, total=False):
    role: str
    content: str


Route = Literal["kb_qa", "troubleshooting", "smalltalk", "out_of_scope"]
AnswerType = Literal["answered", "refusal", "clarify", "smalltalk", "out_of_scope", "degraded"]


class State(TypedDict, total=False):
    # --- 入口 ---
    request_id: str
    session_id: str
    turn_index: int
    question: str
    attach_log: str | None
    history: list[HistoryMessage]
    owner_id: str

    # --- router ---
    route: Route
    route_conf: float

    # --- retrieval ---
    rewritten_query: str | None
    chunks: list[RetrievedChunk]
    # 阈值之下的最高分片段（PRD §7.4：拒答时列出，帮用户判断是否文档缺失）。
    #
    # ⚠️ **必须声明在 State 里**：`State` 是 TypedDict，LangGraph 按 schema 过滤
    # 节点返回值 —— 未声明的键会被**静默丢弃**（实测：retrieval 节点 return 了
    # `probe_chunk`，refuse 却永远拿到 None，拒答于是退化成"未找到"）。
    # 与 D2（`retrieval_top_score` 未落库）、`Citation.Index` 未声明同属一类。
    probe_chunk: RetrievedChunk | None

    # --- troubleshoot ---
    tool_calls: list[ToolCallRecord]
    draft_answer: str | None

    # --- evidence ---
    citations: list[Citation]
    check_result: dict[str, Any] | None

    # --- 终止 ---
    final_text: str | None
    answer_type: AnswerType
    # 节点显式指定的 answer_type（优先于 final 的自动判定）。
    # 用途：troubleshoot 的零证据闸门把答案定为 clarify（D1）。
    _force_answer_type: str | None
    # S9：引用越界明细（代码层检测，写进 trace 供复核；不自动删引用）
    citation_overreach: list[dict]

    # --- 横切 ---
    degraded: bool
    degrade_reasons: list[str]
    preempted: list[str]
    terminated_reason: str

    # --- 用量与轨迹 ---
    usage: dict[str, int]
    # 每节点一行（Go 落 run_steps 用），元素形如
    # {"node": "retrieval", "duration_ms": 412, "input_digest": "sha256:…",
    #  "output_summary": "chunks=5, top_score=0.83", "degraded": false}
    steps: list[dict[str, Any]]