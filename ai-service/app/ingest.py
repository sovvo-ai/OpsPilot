"""文档摄取：切片、Embedding、写入不可见 chunk。"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

from app.config import get_config
from app.db import build_dsn

logger = logging.getLogger("app.ingest")

# FR-11 切片参数
TARGET_CHUNK_SIZE = 500  # 目标长度
MIN_CHUNK_SIZE = 200  # 低于此长度的碎片与相邻块合并
MAX_CHUNK_SIZE = 600  # 常规上限
HARD_MAX_CHUNK_SIZE = 700  # 合并碎片时允许的绝对上限（对齐 PRD 300~700 分布口径）
OVERLAP_SIZE = 80  # 块间段落级重叠下限
HARD_CUT_SIZE = 33000  # S1 SPIKE 实测 embedding 输入硬限
EMBEDDING_BATCH_SIZE = 10


class IngestError(Exception):
    """摄取失败，可被映射为任务层错误码。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def normalize_text(text: str) -> str:
    """NFC + 统一换行 + 去 BOM。"""
    import unicodedata

    text = text.replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", text)


def _vector_literal(vec: list[float]) -> str:
    """把浮点向量转成 pgvector 字面值。"""
    return "[" + ",".join(str(round(v, 8)) for v in vec) + "]"


def _parse_headings(text: str) -> list[tuple[int, int, str, int]]:
    """返回 [(level, start, title, line_end), ...]，按 start 排序。"""
    headings = []
    for m in re.finditer(r"^(#{1,3})\s+(.+)$", text, re.MULTILINE):
        level = len(m.group(1))
        title = m.group(2).strip()
        headings.append((level, m.start(), title, m.end()))
    return headings


def _heading_path_at(headings: list[tuple[int, int, str, int]], pos: int) -> str:
    """返回 pos 位置当前生效的标题路径（H1 > H2 > H3）。"""
    stack: list[tuple[int, str]] = []
    for level, start, title, _ in headings:
        if start > pos:
            break
        # 弹出同级或更低级的旧标题
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
    return " > ".join(title for _, title in stack)


def _split_paragraphs(text: str) -> list[tuple[int, int]]:
    """按空行切分为段落，返回 [(start, end), ...]（保留段落间分隔）。

    段落是切片的最小聚合单位：先切段，再按目标长度把相邻段合并成块。
    代码围栏（```）内的空行不参与切段，保证不切断代码块。
    """
    paragraphs: list[tuple[int, int]] = []
    n = len(text)
    seg_start = 0
    in_code = False
    i = 0
    while i < n:
        # 维护代码围栏状态
        if text.startswith("```", i):
            in_code = not in_code
            i += 3
            continue
        # 段落边界：空行（两个及以上换行，允许中间有空格/tab）
        if not in_code and text[i] == "\n":
            j = i
            while j < n and text[j] in "\n\r\t ":
                j += 1
            # 至少包含一个空行（连续两个换行）才算段落分隔
            if j - i >= 2 and "\n" in text[i:j]:
                end = i
                if end > seg_start:
                    paragraphs.append((seg_start, end))
                seg_start = j
                i = j
                continue
        i += 1
    if seg_start < n:
        paragraphs.append((seg_start, n))
    return paragraphs


def _hard_cut(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """对超长段落（> HARD_CUT_SIZE）按硬限切分，切点尽量落在换行处。"""
    pieces: list[tuple[int, int]] = []
    pos = start
    while pos < end:
        limit = pos + HARD_CUT_SIZE
        if limit >= end:
            pieces.append((pos, end))
            break
        # 从硬限往前找一个换行作为切点，最多回退 200 字符
        cut = limit
        for k in range(limit, max(pos, limit - 200), -1):
            if k < end and text[k - 1] == "\n":
                cut = k
                break
        pieces.append((pos, cut))
        pos = cut
    return pieces


def _heading_path_at(headings: list[tuple[int, int, str, int]], pos: int) -> str:
    """返回 pos 位置当前生效的标题路径（H1 > H2 > H3）。"""
    stack: list[tuple[int, str]] = []
    for level, start, title, _ in headings:
        if start > pos:
            break
        # 弹出同级或更低级的旧标题
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
    return " > ".join(title for _, title in stack)


def chunk_text(text: str) -> list[dict[str, Any]]:
    """按 FR-11 切片：标题层级 + 段落边界，目标 400~600。

    策略：
    1. 先按空行切段落（代码块内不切），超长段落按 33000 硬切；
    2. 按目标长度把相邻段落聚合成块；
    3. 末尾不足 MIN_CHUNK_SIZE 的碎片与相邻块合并，避免产生孤立小碎片；
    4. 相邻块之间保留段落级重叠（约 OVERLAP_SIZE）。
    """
    text = normalize_text(text)
    if not text.strip():
        return []

    headings = _parse_headings(text)

    # 1. 段落切分 + 超长硬切
    raw_paras = _split_paragraphs(text)
    paragraphs: list[tuple[int, int]] = []
    for start, end in raw_paras:
        if end - start > HARD_CUT_SIZE:
            paragraphs.extend(_hard_cut(text, start, end))
        else:
            paragraphs.append((start, end))

    if not paragraphs:
        return []

    # 2. 均衡打包：先按总长估算块数 n，再把段落均分到 n 块，
    #    使每块长度接近 TARGET_CHUNK_SIZE（避免贪心造成的尾块碎片或超长块）。
    spans = paragraphs
    total_len = spans[-1][1] - spans[0][0]
    est_by_target = max(1, round(total_len / TARGET_CHUNK_SIZE))
    min_by_hard = max(1, -(-total_len // HARD_MAX_CHUNK_SIZE))  # ceil 除法
    n_blocks = max(est_by_target, min_by_hard)
    per_block = total_len / n_blocks

    groups: list[tuple[int, int]] = []
    cur_start = spans[0][0]
    cur_end = spans[0][1]
    for start, end in spans[1:]:
        # 当前块已达到均衡目标长度，且后面还有段落 → 收尾
        if (cur_end - cur_start) >= per_block and len(groups) < n_blocks - 1:
            groups.append((cur_start, cur_end))
            cur_start, cur_end = start, end
        else:
            cur_end = end
    groups.append((cur_start, cur_end))

    # 3. 收尾重平衡：末块过小时并入前块（若合并后不超 HARD_MAX），
    #    否则前块让出一部分段落给末块，保持两侧都在合理区间。
    if len(groups) >= 2 and (groups[-1][1] - groups[-1][0]) < MIN_CHUNK_SIZE:
        prev_start, _ = groups[-2]
        if groups[-1][1] - prev_start <= HARD_MAX_CHUNK_SIZE:
            groups[-2] = (prev_start, groups[-1][1])
            groups.pop()
        else:
            # 让出末段：把前块的最后一段划给末块
            prev_start, prev_end = groups[-2]
            last_start, last_end = groups[-1]
            if prev_end - last_start > 0 and (prev_end - prev_start) > MIN_CHUNK_SIZE:
                groups[-2] = (prev_start, last_start)
                groups[-1] = (last_start, last_end)

    # 3b. 硬限保护：任何仍超过 HARD_CUT_SIZE 的块按硬限再切，
    #     确保单条 Embedding 输入永不超限。
    safe_groups: list[tuple[int, int]] = []
    for g_start, g_end in groups:
        if g_end - g_start > HARD_CUT_SIZE:
            safe_groups.extend(_hard_cut(text, g_start, g_end))
        else:
            safe_groups.append((g_start, g_end))
    groups = safe_groups

    # 4. 生成切片（含相邻块重叠，重叠不得突破硬限）
    chunks: list[dict[str, Any]] = []
    for idx, (g_start, g_end) in enumerate(groups):
        # 非首块向左扩展一段重叠，避免边界信息丢失
        start = g_start
        if idx > 0 and OVERLAP_SIZE > 0:
            start = max(g_start - OVERLAP_SIZE, groups[idx - 1][0])
        # 重叠后仍不得突破硬限
        if g_end - start > HARD_CUT_SIZE:
            start = g_end - HARD_CUT_SIZE
        content = text[start:g_end]
        chunks.append(
            {
                "chunk_index": idx,
                "heading_path": _heading_path_at(headings, g_start),
                "char_start": start,
                "char_end": g_end,
                "content": content,
            }
        )
    return chunks


def _build_client(timeout: float) -> httpx.Client:
    """构建外呼 HTTP 客户端。

    容器内 `127.0.0.1` 指向容器自身，宿主机注入的代理变量（如 Docker Desktop
    从宿主继承的 `http_proxy=127.0.0.1:xxxx`）在容器内必然连不通。因此外呼
    默认**不继承环境代理**（`trust_env=False`），只有显式配置
    `OUTBOUND_PROXY` 时才走代理。这与 Go 侧 dispatcher 的
    `Transport{Proxy: nil}` 保持一致。
    """
    proxy = os.getenv("OUTBOUND_PROXY", "").strip()
    if proxy:
        return httpx.Client(timeout=timeout, trust_env=False, proxy=proxy)
    return httpx.Client(timeout=timeout, trust_env=False)


def embed_texts(texts: list[str], cfg: dict) -> list[list[float]]:
    """批量 Embedding，单批 ≤10，429 指数退避。"""
    if not texts:
        return []

    url = cfg["bailian_base_url"].rstrip("/") + "/embeddings"
    headers = {
        "Authorization": f"Bearer {cfg['dashscope_api_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": cfg["embedding_model"],
        "input": texts,
        "encoding_format": "float",
    }

    delays = [1.0, 2.0, 4.0]
    last_err: Exception | None = None
    for attempt, delay in enumerate(delays):
        try:
            t0 = time.monotonic()
            with _build_client(60.0) as client:
                resp = client.post(url, headers=headers, json=payload)
                logger.debug(
                    "embedding 请求 attempt=%d batch=%d status=%d elapsed=%.2fs",
                    attempt,
                    len(texts),
                    resp.status_code,
                    time.monotonic() - t0,
                )
                if resp.status_code == 429:
                    last_err = IngestError("RATE_LIMITED", f"429 {resp.text}")
                    time.sleep(delay)
                    continue
                resp.raise_for_status()
                data = resp.json()
                return [item["embedding"] for item in data["data"]]
        except httpx.HTTPStatusError as exc:
            raise IngestError("EMBEDDING_FAILED", f"HTTP {exc.response.status_code}: {exc.response.text}") from exc
        except Exception as exc:
            raise IngestError("EMBEDDING_FAILED", str(exc)) from exc

    raise last_err or IngestError("RATE_LIMITED", "embedding 429 退避耗尽")


def ingest_document(doc_id: str, run_token: str, attempt: int) -> dict[str, Any]:
    """读取原文 → 切片 → Embedding → 写不可见 chunk。

    对齐 docs/architecture/ingest-consistency.md §4.1：在 Python 自己的事务内
    先 `DELETE FROM doc_chunks WHERE doc_id = $doc_id`（清全世代，含旧世代与孤儿），
    再以 `visible = FALSE` 写入本次 run 的切片。这样既避免确定性 chunk_id
    （uuidv5(doc_id+chunk_index)）与旧世代主键冲突，也保证发布前检索不可见。
    """
    cfg = get_config()
    dsn = build_dsn()

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT content FROM document_contents WHERE doc_id = %s",
                (doc_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise IngestError("PARSE_ERROR", "document_contents 不存在")
            content = row["content"]

    chunks = chunk_text(content)
    logger.info(
        "切片完成 doc_id=%s content_len=%d chunks=%d",
        doc_id,
        len(content),
        len(chunks),
    )
    if not chunks:
        raise IngestError("EMPTY_CONTENT", "切片后无内容")

    # 批量 embedding
    texts = [c["content"] for c in chunks]
    embeddings: list[list[float]] = []
    for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = texts[i : i + EMBEDDING_BATCH_SIZE]
        embeddings.extend(embed_texts(batch, cfg))

    if len(embeddings) != len(chunks):
        raise IngestError("EMBEDDING_FAILED", "embedding 返回数量与切片不匹配")

    doc_uuid = uuid.UUID(doc_id)
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # 阶段一：清全世代（含旧世代与孤儿），避免确定性 chunk_id 主键冲突
            cur.execute("DELETE FROM doc_chunks WHERE doc_id = %s", (doc_id,))
            for chunk, emb in zip(chunks, embeddings):
                chunk_id = uuid.uuid5(doc_uuid, str(chunk["chunk_index"]))
                cur.execute(
                    """
                    INSERT INTO doc_chunks (
                        chunk_id, doc_id, run_token, visible, chunk_index,
                        heading_path, char_start, char_end, content, embedding
                    ) VALUES (
                        %s, %s, %s, FALSE, %s,
                        %s, %s, %s, %s, %s::vector
                    )
                    """,
                    (
                        str(chunk_id),
                        doc_id,
                        run_token,
                        chunk["chunk_index"],
                        chunk["heading_path"],
                        chunk["char_start"],
                        chunk["char_end"],
                        chunk["content"],
                        _vector_literal(emb),
                    ),
                )
            conn.commit()

    return {"run_token": run_token, "status": "ok", "chunk_count": len(chunks)}
