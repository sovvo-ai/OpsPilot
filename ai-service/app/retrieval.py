"""知识检索：查询向量化 → 余弦 top-k → 双重过滤（FR-14/15/16）。

检索过滤（FR-16，四个条件缺一不可）：
  doc_chunks.visible = TRUE
  ∧ ingest_jobs.status = 'completed'
  ∧ documents.deleted_at IS NULL
  ∧ documents.owner_id = $owner
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.db import build_dsn
from app.ingest import _build_client, _vector_literal

logger = logging.getLogger("app.retrieval")


class RetrievalError(Exception):
    """检索失败。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def embed_query(text: str, cfg: dict) -> list[float]:
    """把查询语句向量化（与摄取使用同一模型/接口）。"""
    url = cfg["bailian_base_url"].rstrip("/") + "/embeddings"
    headers = {
        "Authorization": f"Bearer {cfg['dashscope_api_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": cfg["embedding_model"],
        "input": [text],
        "encoding_format": "float",
    }
    try:
        with _build_client(30.0) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["data"][0]["embedding"]
    except Exception as exc:  # noqa: BLE001
        raise RetrievalError("EMBEDDING_FAILED", f"查询向量化失败: {exc}") from exc


def retrieve(
    owner_id: str,
    query: str,
    cfg: dict,
    top_k: int | None = None,
    min_score: float | None = None,
    *,
    probe: bool = False,
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """检索 top_k 切片，按余弦相似度降序，仅返回 score ≥ min_score 的结果。

    返回元素结构（对齐 agent-workflow §2.1 RetrievedChunk）：
      {chunk_id, doc_id, doc_title, score, content, heading_path}

    **`probe=True`（S9 实测修出的缺口）**：返回 `(chunks, best_below)` 二元组，
    其中 `best_below` 是**阈值之下的最高分样本**（无则 `None`）。

    为什么需要它：PRD §7.4 硬要求拒答时"**列出它检索到的最高分片段及分数**"，
    让用户判断是文档缺失还是没问对。但早期实现只返回过阈值的结果 ——
    低分场景下 `chunks` 为空，refuse 只能输出干巴巴的"未找到"，
    **那段"最高分片段"永远拿不到**（实测：`公司差旅报销流程是什么？` 走
    `chunks=0` 分支）。探针不参与作答，只服务拒答展示。
    """
    top_k = top_k or cfg["retrieval_top_k"]
    min_score = cfg["retrieval_min_score"] if min_score is None else min_score

    vec = embed_query(query, cfg)
    literal = _vector_literal(vec)
    dsn = build_dsn()
    ef_search = int(cfg.get("hnsw_ef_search", 100))

    sql = """
        SELECT c.chunk_id, c.doc_id, d.title AS doc_title,
               c.content, c.heading_path,
               1 - (c.embedding <=> %s::vector) AS score
          FROM doc_chunks c
          JOIN ingest_jobs j ON j.doc_id = c.doc_id
          JOIN documents d ON d.id = c.doc_id
         WHERE c.visible = TRUE
           AND j.status = 'completed'
           AND d.deleted_at IS NULL
           AND d.owner_id = %s
         ORDER BY c.embedding <=> %s::vector
         LIMIT %s
    """
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            # 查询期 HNSW 参数（S1-A2：默认 40 召回不足）
            cur.execute(f"SET LOCAL hnsw.ef_search = {ef_search}")
            cur.execute(sql, (literal, owner_id, literal, top_k))
            rows = cur.fetchall()

    chunks: list[dict[str, Any]] = []
    best_below: dict[str, Any] | None = None
    for r in rows:                      # rows 已按相似度降序
        score = float(r["score"])
        item = {
            "chunk_id": str(r["chunk_id"]),
            "doc_id": str(r["doc_id"]),
            "doc_title": r["doc_title"],
            "score": round(score, 4),
            "content": r["content"],
            "heading_path": r["heading_path"] or "",
        }
        if score < min_score:
            # 阈值之下的**第一条**即"最高分探针"（rows 降序）
            if probe and best_below is None:
                best_below = item
            continue
        chunks.append(item)
    logger.info(
        "检索完成 owner=%s query_len=%d 命中=%d 阈值=%.2f 探针=%s",
        owner_id,
        len(query),
        len(chunks),
        min_score,
        f"{best_below['score']}" if best_below else "无",
    )
    if probe:
        return chunks, best_below
    return chunks


def top_score(chunks: list[dict[str, Any]]) -> float:
    """返回最高分（无结果时为 0.0）。"""
    return max((c["score"] for c in chunks), default=0.0)