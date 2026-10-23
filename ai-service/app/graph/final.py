"""终止节点：final / refuse / smalltalk（agent-workflow §3.5/§3.6）。"""

from __future__ import annotations

import hashlib
import logging
import time

from app.chat import (
    SNIPPET_MAX,
    REFUSAL_TEXT_NO_CHUNK,
    build_prompt,
    decide_answer_type,
    extract_citations,
    validate_citations,
)
from app.graph.budget import PRIORITY_FINAL, Budget

logger = logging.getLogger("app.graph.final")

SMALLTALK_TEXT = "你好！我是运维知识助手，可以帮你查文档、排查故障。有什么可以帮你的？"
OUT_OF_SCOPE_TEXT = "这个问题超出了我当前知识库的范围，我只处理技术文档问答与故障排查。"


def smalltalk_node(state: dict, *, record_step=None) -> dict:
    """smalltalk / out_of_scope 终止节点（纯代码，零模型调用）。"""
    t0 = time.monotonic()
    route = state.get("route", "smalltalk")
    if route == "out_of_scope":
        text, atype = OUT_OF_SCOPE_TEXT, "out_of_scope"
    else:
        text, atype = SMALLTALK_TEXT, "smalltalk"

    step = {
        "node": "smalltalk",
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "input_digest": hashlib.sha256(str(state.get("question", "")).encode()).hexdigest()[:16],
        "output_summary": f"route={route}, answer_type={atype}",
        "degraded": bool(state.get("degraded", False)),
    }
    if record_step:
        record_step(step)

    return {"final_text": text, "answer_type": atype, "citations": []}


def refuse_node(state: dict, *, record_step=None) -> dict:
    """refuse 终止节点（纯代码）。

    必须附"最高分片段 + 分数"（PRD §7.4），让用户判断是不是文档缺失。
    """
    t0 = time.monotonic()
    chunks = state.get("chunks") or []
    if not chunks:
        text = REFUSAL_TEXT_NO_CHUNK
    else:
        top = chunks[0]
        snippet = top["content"].strip().replace("\n", " ")
        if len(snippet) > SNIPPET_MAX:
            snippet = snippet[:SNIPPET_MAX]
        text = (
            "未在知识库中找到足够相关的资料来回答该问题。\n\n"
            f"以下是我检索到的最高分片段（分数 {top['score']}），你可以判断是否是文档缺失：\n"
            f"> 来源：{top['doc_title']}\n> {snippet}"
        )

    step = {
        "node": "refuse",
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "input_digest": hashlib.sha256(str(state.get("question", "")).encode()).hexdigest()[:16],
        "output_summary": f"chunks={len(chunks)}, answer_type=refusal",
        "degraded": bool(state.get("degraded", False)),
    }
    if record_step:
        record_step(step)

    return {"final_text": text, "answer_type": "refusal", "citations": []}


def final_node(
    state: dict,
    *,
    cfg: dict,
    budget: Budget,
    llm_call=None,
    record_step=None,
) -> dict:
    """final 节点（LLM，可直通）。

    直通条件：`route=kb_qa` ∧ `check_result.supported=true` ∧ `degraded=false`
             → 直接用 `draft_answer`，**不调模型**（A5"确定性节点零调用"的延伸）。

    预算：final 恒可获取额度（reserved_for_final），A6 要求"Final 永不被抢"。
    """
    t0 = time.monotonic()
    degraded = bool(state.get("degraded", False))
    reasons: list[str] = list(state.get("degrade_reasons") or [])
    citations = list(state.get("citations") or [])
    chunks = state.get("chunks") or []
    draft = state.get("draft_answer") or ""
    check = state.get("check_result") or {}
    route = state.get("route", "kb_qa")

    direct_pass = (
        route == "kb_qa"
        and bool(check.get("supported"))
        and not degraded
        and bool(draft)
    )

    answer = draft
    llm_used = False

    if not direct_pass:
        # 需调模型；final 恒可获取额度（预留份额）
        try:
            ok = budget.try_acquire("final", PRIORITY_FINAL, is_final=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("final 无法获取额度: %s", exc)
            ok = False

        if ok and llm_call is not None:
            prompt = build_prompt(state.get("question", ""), chunks, state.get("history") or [])
            try:
                answer = llm_call("final", prompt)
                budget.consume_llm()
                llm_used = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("final 生成失败，用 draft 兜底: %s", exc)
                degraded = True
                if "final_timeout" not in reasons:
                    reasons.append("final_timeout")
        if not answer:
            answer = draft or "未能在预算内完成回答。"

    # 引用提取 + 代码层校验
    if llm_used or not citations:
        citations = extract_citations(answer or "", chunks)
    citations, invalid = validate_citations(citations, chunks)
    if invalid:
        logger.warning("final 剔除无效引用: %s", invalid)
        degraded = True
        if "invalid_citation_removed" not in reasons:
            reasons.append("invalid_citation_removed")

    answer_type = decide_answer_type(answer or "", citations, has_chunks=bool(chunks))
    if answer_type == "refusal":
        citations = []  # FR-18③：拒答禁止伪造引用

    step = {
        "node": "final",
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "input_digest": hashlib.sha256((draft or state.get("question", "")).encode()).hexdigest()[:16],
        "output_summary": (
            f"direct_pass={direct_pass}, llm_used={llm_used}, "
            f"citations={len(citations)}, answer_type={answer_type}"
        ),
        "degraded": degraded,
    }
    if record_step:
        record_step(step)

    return {
        "final_text": answer,
        "citations": citations,
        "answer_type": answer_type,
        "degraded": degraded,
        "degrade_reasons": reasons,
    }