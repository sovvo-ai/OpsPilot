"""retrieval 节点：纯代码 + 可选 rewrite 子步骤（agent-workflow §3.2 / FR-17）。

rewrite 触发条件（**三重全部满足**，否则不调模型）：
  1. `turn_index ≥ 1`
  2. 命中指代词典（它/那/这个/还有/继续/上面说的…）
  3. `history` 中存在 assistant 消息

失败（超时/空输出）→ `rewritten_query=None` + `degraded=true` + `["rewrite_failed"]`，不阻断。

**首轮不复写**：`turn_index == 0` 时无论是否命中指代词，一律不触发 rewrite（有单测）。
"""

from __future__ import annotations

import hashlib
import logging
import time

from app.graph.budget import PRIORITY_REWRITE, Budget
from app.retrieval import RetrievalError, retrieve, top_score

logger = logging.getLogger("app.graph.retrieval")

# 指代词典（PRD FR-17 / agent-workflow §3.2）
ANAPHORA_WORDS = ("它", "那", "这个", "那个", "还有", "继续", "上面说的", "刚才", "前面说的", "此")


def _starts_with_anaphora(text: str) -> bool:
    """改写结果是否仍以指代词开头（判定消解失败）。"""
    t = text.strip()
    return any(t.startswith(w) for w in ("它", "他", "她", "那", "这个", "那个", "该", "此"))


def should_rewrite(state: dict) -> bool:
    """FR-17 三重条件判定。"""
    turn_index = int(state.get("turn_index", 0))
    if turn_index < 1:
        return False  # 条件 1：首轮不复写
    question = state.get("question", "")
    if not any(w in question for w in ANAPHORA_WORDS):
        return False  # 条件 2：命中指代词
    history = state.get("history") or []
    if not any(m.get("role") == "assistant" for m in history):
        return False  # 条件 3：存在 assistant 消息
    return True


def retrieval_node(
    state: dict,
    *,
    cfg: dict,
    budget: Budget,
    llm_call=None,
    record_step=None,
    retrieve_fn=None,
) -> dict:
    """retrieval 节点实现。

    retrieve_fn: 便于测试注入；默认走 app.retrieval.retrieve。
    """
    t0 = time.monotonic()
    question = state.get("question", "")
    owner_id = state.get("owner_id", "")
    route = state.get("route", "kb_qa")

    rewritten: str | None = None
    degraded = bool(state.get("degraded", False))
    reasons: list[str] = list(state.get("degrade_reasons") or [])

    # --- rewrite 子步骤（非节点）---
    if should_rewrite(state) and llm_call is not None:
        if budget.try_acquire("rewrite", PRIORITY_REWRITE):
            try:
                history_text = "\n".join(
                    f"{'用户' if m.get('role') == 'user' else '助手'}：{m.get('content', '')}"
                    for m in (state.get("history") or [])
                )
                out = llm_call(
                    "rewrite",
                    "把用户的追问改写成**独立完整、可单独检索**的问题。\n"
                    "要求：\n"
                    "1. 必须把指代词（它/那/这个/该…）替换为【最近对话】中的**具体名词**；\n"
                    "2. 不要保留任何指代词，也不要输出解释、前缀或引号；\n"
                    "3. 只输出改写后的一句话。\n\n"
                    f"【最近对话】\n{history_text}\n\n"
                    f"【追问】{question}\n\n"
                    "【改写后的问题】",
                )
                if out and out.strip():
                    candidate = out.strip().strip('"“”\'')
                    # 消解失败守卫：改写结果若仍以指代词开头，说明未真正消解，
                    # 此时退回原句（配合 final prompt 的 anaphora_note 兜底），
                    # 避免把"它怎么配"这类无指代对象的句子送进检索。
                    if candidate and not _starts_with_anaphora(candidate):
                        rewritten = candidate
                    else:
                        logger.info("rewrite 未消解指代（%r），退回原句", candidate[:30])
                budget.consume_llm()
            except Exception as exc:  # noqa: BLE001
                logger.warning("rewrite 失败，用原句继续: %s", exc)
                degraded = True
                if "rewrite_failed" not in reasons:
                    reasons.append("rewrite_failed")
        else:
            # 被抢占：走"无模型分支"，不阻断
            pass

    query = rewritten or question

    # --- 检索（纯代码，零模型调用）---
    fn = retrieve_fn or retrieve
    try:
        chunks = fn(owner_id, query, cfg)
    except RetrievalError as exc:
        logger.warning("检索失败: %s", exc.message)
        degraded = True
        if "embedding_failed" not in reasons:
            reasons.append("embedding_failed")
        chunks = []

    best = top_score(chunks)
    step = {
        "node": "retrieval",
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "input_digest": _digest(query),
        "output_summary": (
            f"chunks={len(chunks)}, top_score={best:.4f}, rewritten={rewritten is not None}"
        ),
        "degraded": degraded,
    }
    if record_step:
        record_step(step)

    return {
        "rewritten_query": rewritten,
        "chunks": chunks,
        "degraded": degraded,
        "degrade_reasons": reasons,
    }


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]