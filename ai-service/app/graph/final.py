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
    detect_citation_overreach,
    normalize_ref_marks,
)
from app.graph.budget import PRIORITY_FINAL, Budget

logger = logging.getLogger("app.graph.final")

SMALLTALK_TEXT = "你好！我是运维知识助手，可以帮你查文档、排查故障。有什么可以帮你的？"
OUT_OF_SCOPE_TEXT = "这个问题超出了我当前知识库的范围，我只处理技术文档问答与故障排查。"

# D1（PRD §7.4 第三行）：追问脱离上下文 → 给 2~3 个候选、**不猜**。
# 与 troubleshoot 零证据闸门的文案保持一致（同一语义不应有两种说法）。
_CLARIFY_TEXT = (
    "信息不足，我需要再确认一下才能回答。\n\n"
    "请补充以下任一信息：\n"
    "1. **具体服务/组件名**（如 Nginx、MySQL、某个 Pod），以及它的报错原文；\n"
    "2. **错误日志片段**（含时间戳与 error 行），我可以直接分析；\n"
    "3. **端口号与现象**（如「8080 起不来」「3306 连接被拒」），便于判断是否端口冲突。\n\n"
    "你指的是哪一种？"
)


def smalltalk_node(state: dict, *, record_step=None) -> dict:
    """smalltalk / out_of_scope / **clarify** 终止节点（纯代码，零模型调用）。

    D1：裸指代（`它起不来了怎么办？`）由 router 硬否决后也落到本节点，
    通过 `_force_answer_type="clarify"` 区分，输出 §7.4 要求的候选清单。
    """
    t0 = time.monotonic()
    forced = state.get("_force_answer_type")

    if forced == "clarify":
        text, atype = _CLARIFY_TEXT, "clarify"
    else:
        route = state.get("route", "smalltalk")
        if route == "out_of_scope":
            text, atype = OUT_OF_SCOPE_TEXT, "out_of_scope"
        else:
            text, atype = SMALLTALK_TEXT, "smalltalk"

    step = {
        "node": "smalltalk",
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "input_digest": hashlib.sha256(str(state.get("question", "")).encode()).hexdigest()[:16],
        "output_summary": f"route={state.get('route')}, answer_type={atype}",
        "degraded": bool(state.get("degraded", False)),
    }
    if record_step:
        record_step(step)

    return {"final_text": text, "answer_type": atype, "citations": []}


def refuse_node(state: dict, *, record_step=None) -> dict:
    """refuse 终止节点（纯代码）。

    **D1 修正**：PRD §7.4 把"拒绝"分成两种不同情况，早期实现混为一谈：

      | 情况 | 应输出 | answer_type |
      |---|---|---|
      | 检索全部低于阈值（**文档可能缺失**） | 最高分片段 + 分数 | `refusal` |
      | 追问脱离上下文（**指代不明**） | 2~3 个候选、不猜 | `clarify` |

    实测 q025（`它起不来了怎么办？`，无上下文）早期走的是第一条文案
    "未在知识库中找到…"，语义不对——用户的问题不是"文档缺失"，
    而是"不知道你在问什么"。
    """
    t0 = time.monotonic()
    chunks = state.get("chunks") or []
    question = state.get("question", "")

    from app.graph.router import _is_bare_anaphora, _is_missing_material

    # 信息不足 → clarify（§7.4 第三行）；两种情况：
    #   ① 追问脱离上下文（裸指代，无 history）
    #   ② 显式要求看材料（"帮我看看这段日志"）但 attach_log 为空 → 缺输入
    if (not state.get("history")) and (
        _is_bare_anaphora(question) or _is_missing_material(state)
    ):
        text = _CLARIFY_TEXT
        answer_type = "clarify"
    elif not chunks:
        # PRD §7.4 硬要求：拒答必须"**列出最高分片段及分数**"，让用户判断是
        # 文档缺失还是没问对。低于阈值的结果被检索层过滤掉了，所以这里用
        # `probe_chunk`（阈值下的最高分样本，S9 实测补上）。
        probe = state.get("probe_chunk")
        if probe:
            snippet = str(probe["content"]).strip().replace("\n", " ")
            if len(snippet) > SNIPPET_MAX:
                snippet = snippet[:SNIPPET_MAX]
            text = (
                "未在知识库中找到与该问题相关的资料（检索到的内容均低于相似度阈值）。\n\n"
                f"以下是我检索到的最高分片段（分数 {probe['score']}，低于阈值），"
                "你可以判断是否是文档缺失：\n"
                f"> 来源：{probe['doc_title']}\n> {snippet}"
            )
        else:
            text = REFUSAL_TEXT_NO_CHUNK
        answer_type = "refusal"
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
        answer_type = "refusal"

    step = {
        "node": "refuse",
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "input_digest": hashlib.sha256(str(state.get("question", "")).encode()).hexdigest()[:16],
        "output_summary": f"chunks={len(chunks)}, answer_type={answer_type}",
        "degraded": bool(state.get("degraded", False)),
    }
    if record_step:
        record_step(step)

    return {"final_text": text, "answer_type": answer_type, "citations": []}


def _accepts_on_delta(fn) -> bool:
    """判断 llm_call 是否支持 `on_delta` 关键字参数（S9 流式能力探测）。

    生产实现（`make_llm_call`）支持；测试用的假 LLM 多为 2 参数闭包。
    不做探测会在单测里抛 TypeError，把"能力缺失"误报成"生成失败"。
    """
    try:
        import inspect

        sig = inspect.signature(fn)
        params = sig.parameters
        if "on_delta" in params:
            return True
        # 有 **kwargs 也算支持
        return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        return False


def final_node(
    state: dict,
    *,
    cfg: dict,
    budget: Budget,
    llm_call=None,
    record_step=None,
    on_delta=None,
) -> dict:
    """final 节点（LLM，可直通）。

    直通条件：`route=kb_qa` ∧ `check_result.supported=true` ∧ `degraded=false`
             → 直接用 `draft_answer`，**不调模型**（A5"确定性节点零调用"的延伸）。

    预算：final 恒可获取额度（reserved_for_final），A6 要求"Final 永不被抢"。

    `on_delta`（S9 流式）：透传给 `llm_call`，使答案**逐块**产出。
    为 None 时走非流式（行为与 S8 完全一致）。
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

    # S7：troubleshoot 已基于**工具证据**产出诊断草稿时，final 优先复用，
    # 不重新凭空生成 —— 否则工具分析结果会被丢弃（实测踩过：模型回问
    # "请提供日志"，尽管 analyze_error_log 已把 error_type 解析出来了）。
    troubleshoot_draft = route == "troubleshooting" and bool(draft)
    if troubleshoot_draft:
        direct_pass = direct_pass or not degraded

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
            # B2 修正：不再把 check_result.supported（可能是 None 或假阴性）注入 prompt。
            # 硬约束只有一条 —— 不得引用不在本次检索集内的片段（由代码层裁剪保证）。
            invalid = list(check.get("invalid_citations") or [])
            prompt = build_prompt(
                state.get("question", ""),
                chunks,
                state.get("history") or [],
                invalid_citations=invalid,
            )
            try:
                # S9：透传 on_delta 走流式；但测试里的假 LLM 只接受 2 参数，
                # 故按签名能力调用（不支持的自动回落非流式）。
                if on_delta is not None and _accepts_on_delta(llm_call):
                    answer = llm_call("final", prompt, on_delta=on_delta)
                else:
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

    # D1：troubleshoot 节点的零证据闸门会给出澄清文案并显式标记 answer_type，
    # 此处优先采纳（否则会被 decide_answer_type 按"有 chunks 就 answered"覆盖）。
    forced = state.get("_force_answer_type")
    if forced:
        answer_type = forced
        citations = []   # 澄清/拒答不携带引用（FR-18③ 同理）

    # B2 修正：answered 但模型漏标 `[n]` → **兜底归因到最高分片段**。
    # 理由：answer_type 已按语义判为 answered，此时无引用会让 A3/A3b 与前端"引用可展开"
    # 缺失。归因对象取本次检索集最高分片段（真实存在、属于本次检索集，满足 A3 硬断言），
    # 且不伪造不存在的 chunk_id。
    if answer_type == "answered" and not citations and chunks:
        top = chunks[0]
        snippet = top["content"].strip().replace("\n", " ")
        if len(snippet) > SNIPPET_MAX:
            snippet = snippet[:SNIPPET_MAX]
        citations = [
            {
                "chunk_id": top["chunk_id"],
                "doc_id": top["doc_id"],
                "doc_title": top["doc_title"],
                "snippet": snippet,
                "score": top["score"],
                "source_type": "kb",
                "status": "live",
            }
        ]
        logger.info("answered 但模型未标注引用，兜底归因到最高分片段 %s", top["chunk_id"][:8])

    # 引用标记归一化：模型偶尔输出 `<n>`（实测漂移），统一成 `[n]` 后再抽取/落库，
    # 否则那些引用会被 extract_citations 静默丢弃（正文有角标、右栏对不上）。
    answer = normalize_ref_marks(answer or "")

    if answer_type == "refusal":
        citations = []  # FR-18③：拒答禁止伪造引用

    # S9：引用越界检测（观测性，不自动删引用）—— 来源 S8 A3b m001 实证。
    # 引用 ID 有效率只验"引用是否存在"；本项验"引用是否支撑结论"的**代码可观测切片**：
    # 带 `[n]` 的句子里若出现该片段没有的技术标识符，几乎必然是从模型自身知识带入的。
    overreach = detect_citation_overreach(answer or "", citations, chunks)
    if overreach:
        logger.warning(
            "检测到引用越界 %d 处（引用 ID 有效但内容不支撑）：%s",
            len(overreach),
            [f"[{o['mark']}]{o['tokens_not_in_source']}" for o in overreach[:3]],
        )

    step = {
        "node": "final",
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "input_digest": hashlib.sha256((draft or state.get("question", "")).encode()).hexdigest()[:16],
        "output_summary": (
            f"direct_pass={direct_pass}, llm_used={llm_used}, "
            f"citations={len(citations)}, answer_type={answer_type}"
            + (f", overreach={len(overreach)}" if overreach else "")
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
        "citation_overreach": overreach,
    }