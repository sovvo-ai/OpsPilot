"""健康检查端点。"""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter, Request

from app.config import get_config
from app.db import DatabaseHealth

router = APIRouter()

APP_VERSION = os.getenv("APP_VERSION", "dev")
db_health = DatabaseHealth()


@router.get("/health/live")
def health_live(request: Request) -> dict[str, Any]:
    """进程存活：永远 200。"""
    return {
        "status": "ok",
        "service": "ops-pilot-ai",
        "version": APP_VERSION,
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


@router.get("/health/ready")
def health_ready(request: Request) -> dict[str, Any]:
    """依赖就绪：数据库可连通。"""
    result = _check_all(request)
    if not result["healthy"]:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=503, content=result)
    return result


@router.get("/health/deps")
def health_deps(request: Request) -> dict[str, Any]:
    """依赖明细。"""
    return _check_all(request)


def _check_all(request: Request) -> dict[str, Any]:
    cfg = get_config()
    checks: dict[str, Any] = {}

    db_result = db_health.check()
    checks["database"] = db_result
    db_ok = db_result["status"] == "ok"

    migration = db_health.latest_migration()
    checks["migrations"] = migration
    migration_ok = migration["status"] == "ok"

    llm_status = "unknown"
    llm_detail = "未配置 API 密钥" if not cfg["dashscope_api_key"] else "未做实时探测"
    if cfg["dashscope_api_key"]:
        llm_status = "ok"
        llm_detail = "密钥已配置"
    checks["llm"] = {"status": llm_status, "detail": llm_detail}
    llm_ok = llm_status == "ok"

    embedding_status = "unknown"
    embedding_detail = "未配置 API 密钥" if not cfg["dashscope_api_key"] else "未做实时探测"
    if cfg["dashscope_api_key"]:
        embedding_status = "ok"
        embedding_detail = f"模型={cfg['embedding_model']}, dim={cfg['embedding_dim']}"
    checks["embedding"] = {
        "status": embedding_status,
        "detail": embedding_detail,
    }
    embedding_ok = embedding_status == "ok"

    healthy = db_ok and migration_ok and llm_ok and embedding_ok

    return {
        "status": "ok" if healthy else "degraded",
        "service": "ops-pilot-ai",
        "version": APP_VERSION,
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "healthy": healthy,
        "checks": checks,
    }
