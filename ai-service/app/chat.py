"""非流式问答编排（S5 单节点直链）。

S5 范围（用户指定）：retrieval → prompt → 生成 → 引用提取 → 拒答分支。
S6 才接入 LangGraph 全图（router/evidence/final 等节点）。因此本模块**不引入
LangGraph**，用一个线性函数实现同样的外部契约（TurnRequest → TurnResult）。

契约保持与 openapi-ai.yaml 的 TurnRequest / TurnResult 一致，S6 换全图时
Go 侧与对外接口无需改动。

拒答分支（FR-20 + PRD §7.4）：
  检索为空 或 最高分 < min_score → answer_type=refusal，
  且**附上检索到的最高分片段与分数**（让用户判断是不是文档缺失），
  绝不输出具体排障步骤。

引用规则（FR-18/19/22）：
  只把**本次检索集内**的 chunk 作为引用；生成文本里出现的 chunk 序号若不在
  检索集，一律剔除（代码层强制，不依赖模型自觉）。
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

from app.config import get_config
from app.ingest import _build_client
from app.retrieval import RetrievalError, retrieve, top_score

logger = logging.getLogger("app.chat")

# 引用片段上限（PRD FR-19：snippet ≤ 200 字）
SNIPPET_MAX = 200

# 拒答文案（PRD §7.4：说明"未在知识库找到依据"，并附最高分片段）
REFUSAL_TEXT_NO_CHUNK = (
    "未在知识库中找到与该问题相关的资料。\n\n"
    "请确认相关文档是否已上传并完成摄取；如需排查，建议补充具体文档后重试。"
)
REFUSAL_TEXT_LOW_SCORE = (
    "未在知识库中找到足够相关的资料来回答该问题。\n\n"
    "以下是我检索到的最高分片段（分数 {score}），你可以判断是否是文档缺失：\n"
    "> 来源：{title}\n> {snippet}"
)


class ChatError(Exception):
    """编排失败，可映射为结构化错误码。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Prompt 组装
# ---------------------------------------------------------------------------


def build_prompt(question: str, chunks: list[dict[str, Any]], history: list[dict[str, str]]) -> str:
    """把检索片段 + 最近 4 轮历史拼成生成用 prompt。

    片段按 `[1] [2] …` 编号，要求模型在结论后标注来源编号，便于提取引用。
    """
    ctx_blocks: list[str] = []
    for idx, c in enumerate(chunks, start=1):
        head = f"（{c['heading_path']}）" if c.get("heading_path") else ""
        ctx_blocks.append(f"[{idx}] 来源《{c['doc_title']}》{head}\n{c['content']}")
    context = "\n\n".join(ctx_blocks) if ctx_blocks else "（无检索结果）"

    hist_lines: list[str] = []
    for m in history:
        role = "用户" if m.get("role") == "user" else "助手"
        hist_lines.append(f"{role}：{m.get('content', '')}")
    history_text = "\n".join(hist_lines) if hist_lines else "（无历史）"

    return (
        "你是运维知识助手。请**仅依据下方【知识库片段】**回答问题。\n"
        "规则：\n"
        "1. 每条事实性结论后必须标注来源编号，格式如 `[1]`；无来源支撑的内容不得输出。\n"
        "2. 若片段不足以回答，请直接说明依据不足，不要编造步骤。\n"
        "3. 不要输出与问题无关的内容。\n\n"
        f"【最近对话】\n{history_text}\n\n"
        f"【知识库片段】\n{context}\n\n"
        f"【问题】{question}\n\n"
        "【回答】"
    )


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def llm_generate(prompt: str, cfg: dict) -> tuple[str, dict[str, int]]:
    """调用 DashScope 兼容接口生成回答，返回 (文本, usage)。"""
    url = cfg["bailian_base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg['dashscope_api_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": cfg["llm_model_qa"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": cfg.get("answer_max_tokens", 1024),
        "temperature": 0.2,
        "stream": False,
    }
    try:
        with _build_client(float(cfg.get("llm_timeout_seconds", 30))) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise ChatError("MODEL_UNAVAILABLE", f"生成调用失败: {exc}") from exc

    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise ChatError("MODEL_BAD_RESPONSE", "生成返回结构异常") from exc

    usage_raw = data.get("usage") or {}
    usage = {
        "prompt_tokens": int(usage_raw.get("prompt_tokens", 0)),
        "completion_tokens": int(usage_raw.get("completion_tokens", 0)),
        "total_tokens": int(usage_raw.get("total_tokens", 0)),
    }
    return text.strip(), usage


# ---------------------------------------------------------------------------
# 引用提取与校验（FR-19 / FR-22）
# ---------------------------------------------------------------------------


def extract_citations(answer: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从回答文本中提取 `[n]` 引用，映射回检索集内的 chunk。

    只保留 `n ∈ [1, len(chunks)]` 的引用；文本里编造的编号一律剔除
    （FR-22：校验在代码层执行，不依赖模型自觉）。
    """
    used_indexes: list[int] = []
    for m in re.finditer(r"\[(\d{1,2})\]", answer):
        n = int(m.group(1))
        if 1 <= n <= len(chunks) and n not in used_indexes:
            used_indexes.append(n)

    citations: list[dict[str, Any]] = []
    for n in used_indexes:
        c = chunks[n - 1]
        snippet = c["content"].strip().replace("\n", " ")
        if len(snippet) > SNIPPET_MAX:
            snippet = snippet[:SNIPPET_MAX]
        citations.append(
            {
                "chunk_id": c["chunk_id"],
                "doc_id": c["doc_id"],
                "doc_title": c["doc_title"],
                "snippet": snippet,
                "score": c["score"],
                "source_type": "kb",
                "status": "live",
            }
        )
    return citations


def validate_citations(
    citations: list[dict[str, Any]], chunks: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """引用存在性校验：每个 chunk_id 必须属于本次检索集。

    返回 (保留的引用, 无效的 chunk_id 列表)。该函数是 A3「引用 ID 有效率 100%」
    的代码层保证。
    """
    valid_ids = {c["chunk_id"] for c in chunks}
    kept: list[dict[str, Any]] = []
    invalid: list[str] = []
    for cit in citations:
        if cit["chunk_id"] in valid_ids:
            kept.append(cit)
        else:
            invalid.append(cit["chunk_id"])
    return kept, invalid


# 答案层拒答特征（第二层判定，见 decide_answer_type 文档）
_REFUSAL_MARKERS = (
    "依据不足",
    "未包含",
    "不包含",
    "无法依据知识库",
    "无法回答",
    "未在知识库",
    "没有相关",
    "找不到",
    "无从判断",
    "无法确定",
)


def _looks_like_refusal(answer: str) -> bool:
    """判断生成文本是否实质为"依据不足"式拒答。

    真实标定发现：正/负样本的检索 top_score 严重重叠（应拒答最高 0.4707，
    应作答最低 0.3860），**纯阈值无法分离**。而模型面对噪声命中时会诚实地说
    "依据不足"。因此引入答案层判定作为第二道闸门。
    """
    return any(m in answer for m in _REFUSAL_MARKERS)


def decide_answer_type(answer: str, citations: list[dict[str, Any]], has_chunks: bool) -> str:
    """判定 answer_type（FR-20 / PRD §7.4）。

    规则（两条闸门）：
      1. 无检索片段 → refusal（上游已处理）
      2. 答案无任何有效引用，**或**文本呈"依据不足"特征 → refusal

    第 2 条是真实标定后的必需项：阈值只能挡住"完全无关"，对"有噪声命中但证据
    不支持"的情况必须靠答案层判定，否则会出现"标 answered 但 0 引用"这种
    A4 判分失败且对用户误导的结果。
    """
    if not has_chunks:
        return "refusal"
    if _looks_like_refusal(answer):
        return "refusal"
    if not citations:
        return "refusal"
    return "answered"


# NOTE(S6)：原 S5 线性 run_turn 已删除 —— 编排统一走 app.graph.run_turn（LangGraph 全图），
# 遵守 agent-workflow §8「两条路径共用同一 run_turn，不允许存在第二套编排代码」。
# 本模块保留 prompt / 引用提取 / 拒答判定等被节点复用的工具函数。