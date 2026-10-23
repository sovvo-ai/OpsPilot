"""内部接口路由（Go → Python），业务逻辑尚未实现。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.middleware import make_error_body

router = APIRouter()


def _not_implemented(request: Request) -> JSONResponse:
    rid = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=501,
        content=make_error_body(
            "NOT_IMPLEMENTED", "该端点已在 OpenAPI 注册，业务逻辑尚未实现", rid
        ),
    )


@router.post("/internal/v1/ingest")
async def ingest(request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.delete("/internal/v1/documents/{doc_id}/vectors")
async def delete_vectors(doc_id: str, request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/internal/v1/chat")
async def chat(request: Request) -> JSONResponse:
    return _not_implemented(request)


@router.post("/internal/v1/chat/stream")
async def chat_stream(request: Request) -> JSONResponse:
    return _not_implemented(request)
