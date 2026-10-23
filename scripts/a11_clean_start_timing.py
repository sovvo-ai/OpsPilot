"""A11 干净环境计时：down -v → up -d → 冒烟全过，分阶段计时。

对照目标："干净环境到冒烟 ≤ 5 分钟"。
超过就**如实写超了和原因**，不修饰。

运行（项目根目录）：
    ai-service/.venv/Scripts/python.exe scripts/a11_clean_start_timing.py

注意：本脚本会 `docker compose down -v`（**清空数据卷**），请确认无重要数据。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = PROJECT_ROOT / "deploy"
ENV_FILE = DEPLOY_DIR / ".env"
OUT_FILE = PROJECT_ROOT / ".workbuddy" / "a11_timing.json"

TARGET_SECONDS = 300  # ≤ 5 分钟


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
    return env


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 600) -> tuple[int, str]:
    t0 = time.monotonic()
    p = subprocess.run(cmd, cwd=str(cwd or PROJECT_ROOT), capture_output=True,
                       text=True, timeout=timeout)
    return int((time.monotonic() - t0) * 1000), (p.stdout + p.stderr)


def wait_ready(url: str, timeout: int = 180, interval: float = 1.0) -> tuple[bool, int]:
    """轮询就绪探针，返回 (是否就绪, 耗时 ms)。"""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            r = httpx.get(url, timeout=5)
            if r.status_code == 200:
                return True, int((time.monotonic() - t0) * 1000)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(interval)
    return False, int((time.monotonic() - t0) * 1000)


def main() -> int:
    env = load_env()
    os.environ.update({k: v for k, v in env.items() if k not in os.environ})
    backend = os.getenv("BACKEND_URL", "http://127.0.0.1:8081")
    ai = os.getenv("AI_SERVICE_URL", "http://127.0.0.1:8000")

    print("=" * 72)
    print("A11 干净环境计时（down -v → up -d → 冒烟）")
    print("=" * 72)
    print(f"目标: ≤ {TARGET_SECONDS}s（5 分钟）")
    print("⚠️  本脚本会清空数据卷（down -v）")

    phases: dict[str, int] = {}

    # 阶段 0：docker system prune（可选，清构建缓存可能反而变慢，仅记录）
    print("\n--- 阶段 0：docker compose down -v（清空栈与数据卷）---")
    ms, out = run(["docker", "compose", "down", "-v"], cwd=DEPLOY_DIR)
    phases["down_v"] = ms
    print(f"  用时 {ms/1000:.1f}s")
    print("  " + out.strip().splitlines()[-1] if out.strip() else "")

    # 阶段 1：镜像构建（不计入"启动"，但单独记录供参考）
    print("\n--- 阶段 1：docker compose build（镜像构建，单独记录不计入启动）---")
    ms, out = run(["docker", "compose", "build"], cwd=DEPLOY_DIR, timeout=1200)
    phases["build"] = ms
    print(f"  用时 {ms/1000:.1f}s")

    # 阶段 2：up -d
    print("\n--- 阶段 2：docker compose up -d ---")
    ms, out = run(["docker", "compose", "up", "-d"], cwd=DEPLOY_DIR)
    phases["up"] = ms
    print(f"  用时 {ms/1000:.1f}s")

    # 阶段 3：等就绪
    print("\n--- 阶段 3：等待就绪探针 ---")
    ok_pg, ms_pg = wait_ready(f"{backend}/health/ready", timeout=180)
    phases["wait_ready"] = ms_pg
    print(f"  backend /health/ready: {'✅' if ok_pg else '❌'} 用时 {ms_pg/1000:.1f}s")
    ok_ai, ms_ai = wait_ready(f"{ai}/health/ready", timeout=180)
    phases["wait_ai_ready"] = ms_ai
    print(f"  ai-service /health/ready: {'✅' if ok_ai else '❌'} 用时 {ms_ai/1000:.1f}s")

    # 阶段 4：冒烟（上传 5 篇 + 一次真实提问）
    print("\n--- 阶段 4：冒烟（迁移 + 摄取 5 篇 + 一次提问）---")
    t_smoke = time.monotonic()
    smoke_ok = True

    # 4.1 迁移（用绝对路径，避免 cwd 切换导致的相对路径失效）
    py = PROJECT_ROOT / "ai-service" / ".venv" / "Scripts" / "python.exe"
    ms_mig, out_mig = run(
        [str(py), str(PROJECT_ROOT / "scripts" / "run_migrate.py"), "up"],
        cwd=PROJECT_ROOT, timeout=300,
    )
    phases["migrate"] = ms_mig
    print(f"  迁移: {ms_mig/1000:.1f}s")

    # 4.2 摄取 5 篇
    try:
        tok = httpx.post(f"{backend}/api/v1/auth/dev-token",
                         params={"user": "demo-user"}, timeout=10).json()["token"]
        H = {"Authorization": f"Bearer {tok}"}
        seed = PROJECT_ROOT / "scripts" / "seed_docs"
        doc_ids = []
        t_ing = time.monotonic()
        for p in sorted(seed.glob("*.md")):
            with p.open("rb") as f:
                r = httpx.post(f"{backend}/api/v1/documents", headers=H,
                               files={"file": (p.name, f, "text/markdown")}, timeout=30)
            if r.status_code in (201, 202):
                doc_ids.append(r.json()["doc_id"])
        # 轮询
        for did in doc_ids:
            for _ in range(120):
                d = httpx.get(f"{backend}/api/v1/documents/{did}", headers=H, timeout=10).json()
                if d.get("ingest_status") in ("completed", "failed"):
                    break
                time.sleep(1)
        phases["ingest_5docs"] = int((time.monotonic() - t_ing) * 1000)
        print(f"  摄取 5 篇: {phases['ingest_5docs']/1000:.1f}s")

        # 4.3 一次真实提问
        sid = httpx.post(f"{backend}/api/v1/sessions", headers=H,
                         json={"title": "A11 smoke"}, timeout=10).json()["id"]
        t_ask = time.monotonic()
        resp = httpx.post(f"{backend}/api/v1/sessions/{sid}/messages", headers=H,
                          json={"question": "Pod 一直 CrashLoopBackOff 怎么排查？"},
                          timeout=180).json()
        phases["first_ask"] = int((time.monotonic() - t_ask) * 1000)
        answered = (resp.get("message") or {}).get("answer_type") == "answered"
        print(f"  首次提问: {phases['first_ask']/1000:.1f}s answer_type="
              f"{(resp.get('message') or {}).get('answer_type')}")
        smoke_ok = answered
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ 冒烟异常: {exc}")
        smoke_ok = False

    total_smoke_ms = int((time.monotonic() - t_smoke) * 1000)
    phases["smoke_total"] = total_smoke_ms

    # 计入"启动到冒烟"的部分：up + 等就绪 + 迁移 + 冒烟
    startup_ms = phases["up"] + phases["wait_ready"] + phases["wait_ai_ready"] + total_smoke_ms
    startup_s = startup_ms / 1000

    print("\n" + "=" * 72)
    print("分阶段计时")
    print("=" * 72)
    print(f"  down -v                : {phases['down_v']/1000:7.1f}s")
    print(f"  build（参考，不计入）  : {phases['build']/1000:7.1f}s")
    print(f"  up -d                  : {phases['up']/1000:7.1f}s")
    print(f"  等 backend ready       : {phases['wait_ready']/1000:7.1f}s")
    print(f"  等 ai ready            : {phases['wait_ai_ready']/1000:7.1f}s")
    print(f"  迁移                   : {phases['migrate']/1000:7.1f}s")
    print(f"  摄取 5 篇              : {phases.get('ingest_5docs',0)/1000:7.1f}s")
    print(f"  首次提问               : {phases.get('first_ask',0)/1000:7.1f}s")
    print(f"  ---- 冒烟合计          : {total_smoke_ms/1000:7.1f}s")
    print()
    print(f"  【启动→冒烟通过】      : {startup_s:7.1f}s   目标 ≤ {TARGET_SECONDS}s  "
          f"{'✅ 达标' if startup_s <= TARGET_SECONDS else '❌ 超时'}")

    if startup_s > TARGET_SECONDS:
        print("\n  ⚠️ 超时原因分析：")
        slowest = sorted(
            [(k, v) for k, v in phases.items() if k != "build"],
            key=lambda kv: -kv[1],
        )[:3]
        for k, v in slowest:
            print(f"     - {k}: {v/1000:.1f}s")

    result = {
        "target_seconds": TARGET_SECONDS,
        "startup_to_smoke_seconds": round(startup_s, 1),
        "passed": startup_s <= TARGET_SECONDS and smoke_ok,
        "smoke_ok": smoke_ok,
        "phases_ms": phases,
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已落盘: {OUT_FILE}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())