#!/usr/bin/env python3
"""辅助脚本：从 deploy/.env 读取配置并启动 backend。"""

import os
import subprocess
import sys
import urllib.parse
from pathlib import Path


def load_env(path: Path) -> dict:
    env = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key] = value
    return env


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    env_path = root / "deploy" / ".env"
    cfg = load_env(env_path)

    user = cfg.get("POSTGRES_USER", "ops_pilot")
    password = cfg.get("POSTGRES_PASSWORD", "change-me-local-only")
    host = cfg.get("POSTGRES_HOST", "localhost")
    if host == "postgres":
        host = "localhost"
    port = cfg.get("POSTGRES_HOST_PORT") or "55432"
    db = cfg.get("POSTGRES_DB", "ops_pilot")

    dsn = (
        f"postgres://{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(password, safe='')}"
        f"@{host}:{port}/{db}?sslmode=disable"
    )

    env = os.environ.copy()
    env["DATABASE_URL"] = dsn
    env["AI_SERVICE_URL"] = "http://127.0.0.1:18099"  # 探测一个不存在的 AI 服务
    env["AI_SERVICE_TOKEN"] = cfg.get("AI_SERVICE_TOKEN", "")
    env["JWT_SECRET"] = cfg.get("JWT_SECRET", "")
    env["ENABLE_DEV_AUTH"] = cfg.get("ENABLE_DEV_AUTH", "true")
    env["APP_PORT"] = cfg.get("APP_PORT", "8081")

    cmd = ["go", "run", "./cmd/api"]
    result = subprocess.run(cmd, cwd=root / "backend", env=env)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
