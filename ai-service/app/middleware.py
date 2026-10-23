"""FastAPI 中间件：内部令牌鉴权、request_id 透传、统一错误体。"""

import hmac
import secrets
import time
import uuid
from typing import Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_config


def _internal_token() -> str:
    return get_config()["ai_service_token"]


def check_internal_token(token: str | None) -> bool:
    """常量时间比较内部令牌；缺失/错误/未配置均返回 False。"""
    expected = _internal_token()
    if not expected or not token:
        return False
    return hmac.compare_digest(token, expected)


def make_error_body(code: str, message: str, request_id: str | None = None) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id or str(uuid.uuid4()),
        }
    }


def generate_request_id() -> str:
    return f"{time.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4)}"


class RequestIDMiddleware(BaseHTTPMiddleware):
    """注入或透传 X-Request-Id，并写入响应头。"""

    async def dispatch(self, request: Request, call_next: Callable):
        rid = request.headers.get("X-Request-Id") or generate_request_id()
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        return response


class InternalAuthMiddleware(BaseHTTPMiddleware):
    """对 /internal/** 路由校验 X-Internal-Token；/health/** 豁免。"""

    async def dispatch(self, request: Request, call_next: Callable):
        path = request.url.path
        if path.startswith("/health/"):
            return await call_next(request)

        if path.startswith("/internal/"):
            token = request.headers.get("X-Internal-Token")
            if not check_internal_token(token):
                return JSONResponse(
                    status_code=401,
                    content=make_error_body(
                        "INTERNAL_AUTH_FAILED", "内部令牌无效", getattr(request.state, "request_id", None)
                    ),
                )

        return await call_next(request)
