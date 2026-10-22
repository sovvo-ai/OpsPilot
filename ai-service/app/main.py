"""OpsPilot AI Service — S1 SPIKE 骨架。

只提供 health 级端点与契约验证用端点，不含任何业务逻辑。
"""

import hmac
import json
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

APP_VERSION = "spike"
INTERNAL_TOKEN = os.getenv("AI_SERVICE_TOKEN", "")

app = FastAPI(title="OpsPilot AI Service", version=APP_VERSION)


def _check_internal_token(x_internal_token: str | None) -> None:
    """内部调用鉴权：缺失与错误一律 401，不区分原因、不回显令牌。"""
    if not x_internal_token or not INTERNAL_TOKEN:
        raise HTTPException(status_code=401, detail={"code": "INTERNAL_AUTH_FAILED"})
    if not hmac.compare_digest(x_internal_token, INTERNAL_TOKEN):
        raise HTTPException(status_code=401, detail={"code": "INTERNAL_AUTH_FAILED"})


@app.get("/health/live")
def health_live() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "ops-pilot-ai",
        "version": APP_VERSION,
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


@app.get("/health/deps")
def health_deps() -> dict[str, Any]:
    """S1 阶段只回报配置是否就位，不发起真实出网调用。"""
    return {
        "db": {"status": "NOT_RUN", "detail": "S1 未接数据库"},
        "llm": {"status": "NOT_RUN", "model": os.getenv("LLM_MODEL_QA", "")},
        "embedding": {
            "status": "NOT_RUN",
            "model": os.getenv("EMBEDDING_MODEL", ""),
            "dim": os.getenv("EMBEDDING_DIM", ""),
        },
    }


@app.post("/internal/v1/echo")
async def internal_echo(
    request: Request,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
) -> JSONResponse:
    """契约验证：中文往返 + request_id 透传。"""
    _check_internal_token(x_internal_token)
    body = await request.json()
    return JSONResponse(
        {
            "request_id": x_request_id or str(uuid.uuid4()),
            "echo": body,
            "echo_text": body.get("text", ""),
            "text_len": len(body.get("text", "")),
        }
    )


@app.get("/internal/v1/slow")
def internal_slow(
    ms: int = Query(default=3000, ge=0, le=60000),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> dict[str, Any]:
    """契约验证：Go 侧超时是否按预期触发，而非无限等待。"""
    _check_internal_token(x_internal_token)
    time.sleep(ms / 1000)
    return {"slept_ms": ms}


@app.get("/internal/v1/boom")
def internal_boom(
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> JSONResponse:
    """契约验证：错误码与错误体的透传形态。"""
    _check_internal_token(x_internal_token)
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "INVALID_INGEST_REQUEST",
                "message": "缺少 doc_id",
                "request_id": "spike-fixed-id",
            }
        },
    )
