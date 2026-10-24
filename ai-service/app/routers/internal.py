"""内部接口路由（Go → Python）。"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.chat import ChatError
from app.graph import run_turn
from app.ingest import IngestError, ingest_document
from app.middleware import make_error_body

router = APIRouter()
logger = logging.getLogger("app.routers.internal")


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

    # 【B1 排查用】载荷落地诊断：确认 Go→Python 的 history / turn_index 真实到达情况
    history = body.get("history") or []
    logger.info(
        "chat 载荷: request_id=%s session_id=%s turn_index=%s question=%r "
        "history_len=%d history_roles=%s owner_id=%s attach_log_len=%d",
        body.get("request_id"),
        body.get("session_id"),
        body.get("turn_index"),
        (body.get("question") or "")[:40],
        len(history),
        [m.get("role") for m in history],
        body.get("owner_id"),
        len(body.get("attach_log") or ""),
    )

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
async def chat_stream(request: Request):
    """Go 调用 Python 编排的**流式**入口（S9）。

    PRD §8.5 契约：
    - `meta` → `delta`* → `citation`（流后一次性）→ `done` | `error`
    - **禁止静默断流**（S3）：异常必发 `error`
    - 客户端断开（S4）：停止生成、落 `interrupted` trace、不再烧 token
    - **与非流式共用同一 `run_turn`**（S5）：本函数不复制任何编排逻辑

    返回 `StreamingResponse(text/event-stream)`。
    """
    rid = getattr(request.state, "request_id", None)
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=400,
            content=make_error_body("INVALID_REQUEST", f"请求体解析失败: {exc}", rid),
        )
    if rid:
        body["request_id"] = rid

    async def event_gen():
        """SSE 事件生成器：把 run_turn 的同步 emit 桥接到异步队列。

        `run_turn` 是同步的（LangGraph invoke），故放线程里跑，用队列回传事件；
        生成器侧用 `asyncio.to_thread` 等待队列，避免阻塞事件循环。
        """
        import asyncio
        import queue as _queue
        import threading

        q: _queue.Queue = _queue.Queue()
        SENTINEL = object()
        interrupted = threading.Event()
        result_holder: dict = {}

        def emit(ev: dict) -> None:
            q.put(ev)

        def should_stop() -> bool:
            return interrupted.is_set()

        def worker() -> None:
            try:
                result_holder["result"] = run_turn(
                    body, emit=emit, should_stop=should_stop
                )
            except ChatError as exc:
                result_holder["error"] = ("CHAT_ERROR", exc.code, exc.message)
            except Exception as exc:  # noqa: BLE001
                logger.exception("stream 执行失败")
                result_holder["error"] = ("INTERNAL_ERROR", "INTERNAL_ERROR", str(exc))
            finally:
                q.put(SENTINEL)

        th = threading.Thread(target=worker, daemon=True)
        th.start()

        def sse(ev: dict) -> str:
            return f"event: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"

        try:
            while True:
                # 断连探测（S4）：客户端走了就置位，让 worker 尽快停。
                # 间隔取 50ms：生成器与 worker 是并发的，间隔过大（如 200ms）
                # 会让短回答在"察觉断连"前就跑完（实测踩到过）。
                if await request.is_disconnected():
                    interrupted.set()

                try:
                    ev = await asyncio.to_thread(q.get, True, 0.05)
                except _queue.Empty:
                    continue

                if ev is SENTINEL:
                    break
                yield sse(ev)

            # worker 结束但还没发 done/error → 兜底（**禁止静默断流**）
            if result_holder.get("error"):
                _, code, msg = result_holder["error"]
                yield sse({"type": "error", "code": code, "message": msg})
            else:
                # S4 保险：若已察觉断连、但 done 已发出（生成太快来不及中断），
                # 在**流尾部**补发 error 说明"生成被中断"，避免前端误判为完整回答。
                res = result_holder.get("result") or {}
                if interrupted.is_set() and res.get("terminated_reason") == "completed":
                    yield sse({
                        "type": "error", "code": "CLIENT_DISCONNECTED",
                        "message": "客户端断开，生成已中断",
                    })
        except asyncio.CancelledError:
            # 客户端断开导致生成器被取消：置位并交给 worker 收尾落 trace
            interrupted.set()
            raise
        finally:
            # 等 worker 落完库（trace 必须写成功，否则 U9"断流后 trace"无从验证）
            await asyncio.to_thread(th.join, 30)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # 防中间层缓冲（nginx 类）
            "Connection": "keep-alive",
        },
    )
