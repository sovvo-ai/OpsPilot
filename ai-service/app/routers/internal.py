"""内部接口路由（Go → Python）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.chat import ChatError
from app.graph import run_turn
from app.ingest import IngestError, ingest_document
from app.middleware import make_error_body

router = APIRouter()


@router.post("/internal/v1/ingest")
async def ingest(request: Request) -> JSONResponse:
    """Go 派发任务后调用：切片、Embedding、写入不可见 chunk。"""
    rid = getattr(request.state, "request_id", None)
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=400,
            content=make_error_body("INVALID_REQUEST", f"请求体解析失败: {exc}", rid),
        )

    doc_id = body.get("doc_id")
    run_token = body.get("run_token")
    attempt = body.get("attempt", 0)
    if not doc_id or not run_token:
        return JSONResponse(
            status_code=400,
            content=make_error_body("INVALID_REQUEST", "缺少 doc_id 或 run_token", rid),
        )

    try:
        result = ingest_document(str(doc_id), str(run_token), int(attempt))
        return JSONResponse(status_code=200, content=result)
    except IngestError as exc:
        return JSONResponse(
            status_code=200,
            content={
                "run_token": run_token,
                "status": "error",
                "error_code": exc.code,
                "error_msg": exc.message,
                "chunk_count": 0,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=200,
            content={
                "run_token": run_token,
                "status": "error",
                "error_code": "EMBEDDING_FAILED",
                "error_msg": str(exc),
                "chunk_count": 0,
            },
        )


@router.delete("/internal/v1/documents/{doc_id}/vectors")
async def delete_vectors(doc_id: str, request: Request) -> JSONResponse:
    """按 doc_id 删除全部向量（含 visible=false）。"""
    rid = getattr(request.state, "request_id", None)
    try:
        from app.db import build_dsn

        dsn = build_dsn()
        import psycopg

        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM doc_chunks WHERE doc_id = %s", (doc_id,))
                deleted = cur.rowcount
            conn.commit()
        return JSONResponse(
            status_code=200,
            content={"doc_id": doc_id, "deleted_chunks": deleted},
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=500,
            content=make_error_body("VECTOR_CLEANUP_FAILED", str(exc), rid),
        )


@router.post("/internal/v1/chat")
async def chat(request: Request) -> JSONResponse:
    """Go 调用 Python 编排的入口：非流式执行一轮问答。

    S6 起走 **LangGraph 全图**（`app.graph.run_turn`），与流式接口共用同一入口
    （agent-workflow §8）。业务失败用 200 + 结构化结果表达（TurnResult）；
    5xx 仅用于不可预期的内部错误。
    """
    rid = getattr(request.state, "request_id", None)
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=400,
            content=make_error_body("INVALID_REQUEST", f"请求体解析失败: {exc}", rid),
        )

    # request_id 以中间件透传的为准，保证跨服务串联
    if rid:
        body["request_id"] = rid

    try:
        result = run_turn(body)
        return JSONResponse(status_code=200, content=result)
    except ChatError as exc:
        return JSONResponse(
            status_code=422,
            content=make_error_body(exc.code, exc.message, rid),
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=500,
            content=make_error_body("INTERNAL_ERROR", str(exc), rid),
        )


@router.post("/internal/v1/chat/stream")
async def chat_stream(request: Request) -> JSONResponse:
    rid = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=501,
        content=make_error_body("NOT_IMPLEMENTED", "chat stream 尚未实现（S7）", rid),
    )
