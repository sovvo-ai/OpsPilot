"""生成 demo-api.gif：真实执行命令 → 按 asciicast v2 记录 → 容器内 agg 转 GIF。

**为什么不用 asciinema 客户端**：`asciinema` 依赖 Unix-only 的 `fcntl`，
Windows 上无法运行。`agg`（录制格式转 GIF 的工具）是 Rust 二进制，无平台限制。
因此本脚本**自己实现录制器**（asciicast v2 格式），再用容器里的 agg 转 GIF。

**真实性保证**：
- 每条命令都**真实执行**（`subprocess` + 真实 HTTP 请求），输出是原始 stdout；
- 不做任何摆拍、「预置结果」或事后剪辑伪造；
- 失败重试**不剪掉**（如实呈现）；仅对**空闲等待**做倍速压缩。

用法：
    python scripts/demo_record.py --init      # 首次：初始化演示数据（上传 5 篇）
    python scripts/demo_record.py             # 录制 → docs/demo/demo-api.gif
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT_ROOT / "docs" / "demo"
CAST_PATH = OUT_DIR / "demo-api.cast"
GIF_PATH = OUT_DIR / "demo-api.gif"
PY = PROJECT_ROOT / "ai-service" / ".venv" / "Scripts" / "python.exe"
ENV_FILE = PROJECT_ROOT / "deploy" / ".env"

AGG_IMAGE = "ghcr.io/asciinema/agg:latest"

# 每步：(说明, 命令 argv, 环境变量覆盖, 是否压缩等待)
#
# ⚠️ **含换行的内容一律走环境变量**，不走命令行参数 ——
# Windows 下 subprocess 会把含换行的参数**截断在第一行**
# （实测：`8080 nginx...\n8080 java...` 只剩前半句，导致错误结论）。
DEMO_STEPS: list[tuple[str, list[str], dict[str, str], bool]] = [
    ("# 1) 系统状态：核心服务就绪",
     [str(PY), "-c",
      "import httpx;print('backend /health/ready ->',"
      " httpx.get('http://127.0.0.1:8081/health/ready',timeout=5).status_code)"],
     {}, False),
    ("# 2) 知识库问答（引用绑定真实片段 + 完整 Top-K）",
     [str(PY), "scripts/demo_ask.py", "--show-run", "--title", "demo-kb"],
     {"DEMO_QUESTION": "Nginx 的 502 和 504 分别代表什么？"}, True),
    ("# 3) 带日志排查：工具真的被调用（证据来源标注为『工具』）",
     [str(PY), "scripts/demo_ask.py", "--show-run", "--title", "demo-ts"],
     {"DEMO_QUESTION": "帮我看看这段日志",
      "DEMO_LOG": ("2026-10-23 10:00:01 ERROR dial tcp 10.0.0.1:3306: connect: connection refused\n"
                   "2026-10-23 10:00:02 ERROR failed to connect to mysql: connection refused\n"
                   "2026-10-23 10:00:03 ERROR retry exhausted, exiting with code 1")}, True),
    ("# 4) 端口冲突：同端口两进程（逐行提取，保留同端口多条）",
     [str(PY), "scripts/demo_ask.py", "--show-run", "--title", "demo-port"],
     {"DEMO_QUESTION": "8080 nginx 正在监听\n8080 java 也在用，是不是端口冲突？"}, True),
    ("# 5) 知识库没有的问题 → 拒答（列出最高分片段及分数，不编）",
     [str(PY), "scripts/demo_ask.py", "--show-run", "--title", "demo-refuse"],
     {"DEMO_QUESTION": "公司差旅报销流程是什么？"}, True),
    ("# 6) 追问脱离上下文 → 澄清（给候选，不猜）",
     [str(PY), "scripts/demo_ask.py", "--show-run", "--title", "demo-clarify"],
     {"DEMO_QUESTION": "它起不来了怎么办？"}, False),
]


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
    return env


class Recorder:
    """极简 asciicast v2 录制器。"""

    def __init__(self, width: int = 100, height: int = 32) -> None:
        self.events: list[list] = []
        self.t0 = time.monotonic()
        self.width = width
        self.height = height

    def _t(self) -> float:
        return time.monotonic() - self.t0

    def write(self, data: str) -> None:
        self.events.append([round(self._t(), 4), "o", data])

    def write_slow(self, text: str, delay: float = 0.012) -> None:
        """逐字输出（模拟打字观感；内容本身是真实输出）。"""
        for ch in text:
            self.write(ch)
            time.sleep(delay)

    def line(self, text: str, delay: float = 0.0) -> None:
        self.write(text + "\r\n")
        if delay:
            time.sleep(delay)

    def to_cast(self) -> str:
        header = {
            "version": 2,
            "width": self.width,
            "height": self.height,
            "timestamp": int(time.time()),
            "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"},
        }
        lines = [json.dumps(header, ensure_ascii=False)]
        for e in self.events:
            lines.append(json.dumps(e, ensure_ascii=False))
        return "\n".join(lines) + "\n"


def init_demo_data(env: dict[str, str]) -> None:
    """初始化：上传 5 篇 seed_docs 至 completed。"""
    import httpx

    backend = os.getenv("BACKEND_URL", "http://127.0.0.1:8081")
    tok = httpx.post(f"{backend}/api/v1/auth/dev-token",
                     params={"user": "demo-user"}, timeout=10).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    seed = PROJECT_ROOT / "scripts" / "seed_docs"
    for p in sorted(seed.glob("*.md")):
        with p.open("rb") as f:
            r = httpx.post(f"{backend}/api/v1/documents", headers=h,
                           files={"file": (p.name, f, "text/markdown")}, timeout=30)
        if r.status_code not in (201, 202):
            print(f"  ! {p.name}: {r.status_code} {r.text[:120]}")
            continue
        did = r.json()["doc_id"]
        for _ in range(120):
            d = httpx.get(f"{backend}/api/v1/documents/{did}", headers=h, timeout=10).json()
            if d.get("ingest_status") in ("completed", "failed"):
                print(f"  {p.name}: {d.get('ingest_status')} chunks={d.get('chunk_count')}")
                break
            time.sleep(1)


def run_step(rec: Recorder, note: str, argv: list[str],
             env_over: dict[str, str], compress: bool) -> None:
    """真实执行一步并记录。"""
    rec.line(f"\x1b[90m{note}\x1b[0m")

    # 展示用的命令（环境变量内容太长，改为提示）
    shown = " ".join(argv)
    if env_over:
        q = env_over.get("DEMO_QUESTION", "")
        head = q.splitlines()[0][:40]
        shown += f'   # 提问: "{head}{"…" if len(q) > 40 else ""}"'
    if len(shown) > 100:
        shown = shown[:97] + "..."
    rec.write("\x1b[1;32m$\x1b[0m ")
    rec.write_slow(shown, 0.006)
    rec.line("")

    t0 = time.monotonic()
    child_env = dict(os.environ)
    child_env.update(env_over)
    try:
        p = subprocess.run(argv, cwd=str(PROJECT_ROOT), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=300,
                           env=child_env)
        out = (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        out = f"[执行异常] {exc}\n"
    elapsed = time.monotonic() - t0

    # 真实输出（逐行写入，保持终端观感）
    for ln in out.splitlines():
        rec.write("\x1b[37m" + ln + "\x1b[0m\r\n")
    rec.line("")

    # 空闲压缩：长耗时命令在时间轴上"快进"（观感加速，不改内容）
    if compress and elapsed > 2.0:
        rec.write("")           # 无输出事件，仅推进时间轴
        rec.events.append([round(rec._t() + 1.2, 4), "o", ""])
    time.sleep(0.4)


def to_gif(cast: Path, gif: Path) -> bool:
    """用容器内 agg 把 cast 转 GIF。"""
    out_dir = gif.parent
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{cast.parent}:/data",     # Windows 路径由 docker 自己处理
        AGG_IMAGE,
        "--font-size", "18", "--speed", "1.4",
        "--theme", "asciinema",
        f"/data/{cast.name}", f"/data/{gif.name}",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        print(f"agg 失败: {r.returncode}\n{r.stderr[:600]}")
        return False
    _ = out_dir
    return gif.exists()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true", help="仅初始化演示数据后退出")
    ap.add_argument("--keep-cast", action="store_true", help="保留 .cast 源文件")
    args = ap.parse_args()

    env = load_env()
    os.environ.update({k: v for k, v in env.items() if k not in os.environ})

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("[demo] 初始化演示数据 …")
    init_demo_data(env)
    if args.init:
        print("[demo] 初始化完成")
        return 0

    print("[demo] 开始录制（真实执行）…")
    rec = Recorder()
    rec.line("\x1b[1;36mOpsPilot · 运维知识库问答与故障排查 Agent\x1b[0m")
    rec.line("\x1b[90m演示：真实运行，非摆拍。终端输出为原始 stdout。\x1b[0m")
    rec.line("")

    for note, argv, env_over, compress in DEMO_STEPS:
        run_step(rec, note, argv, env_over, compress)

    rec.line("\x1b[1;33m—— 演示结束 ——\x1b[0m")
    rec.line("\x1b[90m全部输出来自真实接口；报告与逐题结果见 ai-service/evals/reports/\x1b[0m")

    CAST_PATH.write_text(rec.to_cast(), encoding="utf-8")
    size_kb = CAST_PATH.stat().st_size / 1024
    print(f"[demo] cast 已写: {CAST_PATH}（{size_kb:.1f} KB, {len(rec.events)} 事件）")

    print("[demo] 用容器内 agg 转 GIF …")
    ok = to_gif(CAST_PATH, GIF_PATH)
    if ok:
        gif_mb = GIF_PATH.stat().st_size / 1024 / 1024
        print(f"[demo] GIF: {GIF_PATH}（{gif_mb:.2f} MB）")
        if gif_mb > 10:
            print(f"[demo] ⚠️ 超过 10MB 限制（{gif_mb:.2f} MB），需压缩")
    if not args.keep_cast and CAST_PATH.exists():
        CAST_PATH.unlink()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())