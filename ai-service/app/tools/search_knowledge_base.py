"""`search_knowledge_base`：检索已入库文档（tool-policy §2.3 / PRD FR-25）。

**复用 retrieval 函数而非另写** —— 避免"工具能搜到的片段，主链路搜不到"。
"""

from __future__ import annotations

from typing import Any

from app.retrieval import RetrievalError, retrieve
from app.tools.base import ToolError

MAX_TOP_K = 10
MAX_QUERY_CHARS = 500


def search_knowledge_base(args: dict[str, Any]) -> dict[str, Any]:
    """入参：`query`(1–500)、可选 `top_k`(1–10)、`owner_id`（内部注入）。

    注意：SQL 片段注入（如 `'; DROP TABLE doc_chunks; --`）会被当作**普通文本**
    参与检索 —— 检索 SQL 全程参数化，不存在字符串拼接。
    """
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("INVALID_TOOL_ARGS", "query 必须为非空字符串，长度 1–500")
    if len(query) > MAX_QUERY_CHARS:
        raise ToolError("INVALID_TOOL_ARGS", f"query 超长（>{MAX_QUERY_CHARS}）")

    top_k_raw = args.get("top_k", 5)
    if not isinstance(top_k_raw, int) or isinstance(top_k_raw, bool):
        raise ToolError("INVALID_TOOL_ARGS", "top_k 必须是整数")

    truncated_topk = top_k_raw > MAX_TOP_K
    if truncated_topk:
        raise ToolError("INVALID_TOOL_ARGS", f"top_k 必须 ≤ {MAX_TOP_K}")

    owner_id = args.get("owner_id")
    if not owner_id:
        raise ToolError("INVALID_TOOL_ARGS", "缺少 owner_id（检索必须按 owner 过滤）")

    cfg = args.get("_cfg")
    if not cfg:
        from app.config import get_config

        cfg = get_config()

    try:
        chunks = retrieve(owner_id, query, cfg, top_k=top_k_raw, min_score=0.0)
    except RetrievalError as exc:
        raise ToolError("TOOL_ERROR", f"检索失败: {exc.message}") from exc

    return {
        "chunks": [
            {
                "chunk_id": c["chunk_id"],
                "doc_id": c["doc_id"],
                "doc_title": c["doc_title"],
                "score": c["score"],
                "content": c["content"],
            }
            for c in chunks
        ],
        "count": len(chunks),
        "truncated": False,
    }