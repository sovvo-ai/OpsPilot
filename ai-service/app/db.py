"""数据库连接与迁移状态（仅用于健康检查）。"""

from __future__ import annotations

import os
import urllib.parse
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row


def build_dsn() -> str:
    """从环境变量构建 DSN，支持 DATABASE_URL 或 PG* 分变量；密码自动 URL 编码。"""
    if dsn := os.getenv("DATABASE_URL"):
        return dsn

    user = os.getenv("POSTGRES_USER", "ops_pilot")
    password = os.getenv("POSTGRES_PASSWORD", "change-me-local-only")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "55432")
    db = os.getenv("POSTGRES_DB", "ops_pilot")
    sslmode = os.getenv("POSTGRES_SSLMODE", "disable")
    return (
        f"postgres://{urllib.parse.quote(user, safe='')}:"
        f"{urllib.parse.quote(password, safe='')}@{host}:{port}/{db}?sslmode={sslmode}"
    )


class DatabaseHealth:
    """轻量级数据库健康探测，不长期持有连接。"""

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or build_dsn()

    def is_configured(self) -> bool:
        return bool(self.dsn)

    def check(self) -> dict[str, Any]:
        if not self.dsn:
            return {"status": "unknown", "detail": "DATABASE_URL 未配置", "latency_ms": 0}

        import time

        start = time.monotonic()
        try:
            with psycopg.connect(self.dsn, connect_timeout=3) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            latency_ms = int((time.monotonic() - start) * 1000)
            return {"status": "ok", "latency_ms": latency_ms}
        except Exception as exc:  # noqa: BLE001
            latency_ms = int((time.monotonic() - start) * 1000)
            return {"status": "error", "detail": str(exc), "latency_ms": latency_ms}

    def latest_migration(self) -> dict[str, Any]:
        if not self.dsn:
            return {"status": "unknown", "detail": "DATABASE_URL 未配置"}

        try:
            with psycopg.connect(self.dsn, connect_timeout=3, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT version FROM schema_migrations
                        WHERE EXISTS (
                            SELECT 1 FROM information_schema.tables
                            WHERE table_name = 'schema_migrations'
                        )
                        ORDER BY version DESC LIMIT 1
                        """
                    )
                    row = cur.fetchone()
                    if row:
                        return {"status": "ok", "detail": row["version"]}
                    return {"status": "error", "detail": "未应用任何迁移"}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "detail": str(exc)}


@contextmanager
def get_conn() -> Any:
    """业务代码使用的连接上下文（占位）。"""
    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError("DATABASE_URL 未配置")
    with psycopg.connect(dsn) as conn:
        yield conn
