#!/usr/bin/env python3
"""辅助脚本：从 deploy/.env 读取数据库配置并运行 backend 迁移。"""

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
    # 本地开发时覆盖为 localhost + 宿主端口
    if host == "postgres":
        host = "localhost"
    port = cfg.get("POSTGRES_HOST_PORT") or "55432"
    db = cfg.get("POSTGRES_DB", "ops_pilot")

    dsn = (
        f"postgres://{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(password, safe='')}"
        f"@{host}:{port}/{db}?sslmode=disable"
    )

    cmd = ["go", "run", "./cmd/api/migrate", sys.argv[1] if len(sys.argv) > 1 else "up"]
    env = os.environ.copy()
    env["DATABASE_URL"] = dsn
    env["AI_SERVICE_TOKEN"] = cfg.get("AI_SERVICE_TOKEN", "")
    env["JWT_SECRET"] = cfg.get("JWT_SECRET", "")
    result = subprocess.run(cmd, cwd=root / "backend", env=env)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
