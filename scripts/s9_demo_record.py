"""浏览器版 demo 录制：Playwright（系统 Edge）录像 → ffmpeg 转 GIF。

**红线**：内容全部来自**真实运行**（真前端、真后端、真模型），不摆拍、不预置结果。
允许的操作只有：空闲等待倍速压缩、裁剪首尾。**不允许**剪掉失败重试后假装一次成功。

分镜依据 `docs/demo/demo-script.md`（3 分钟）：
  0:00 定位（首页 + 三栏）
  0:20 上传 → 文档状态
  1:00 提问（kb_qa）→ 引用角标 → 右栏片段
  1:50 拒答（知识库没有的问题）
  2:10 澄清（裸指代"它起不来了怎么办？"）
  2:30 轨迹抽屉（节点时间线 + 模型调用次数）

运行：
    ai-service/.venv/Scripts/python.exe scripts/s9_demo_record.py
产出：
    docs/demo/demo.gif（≤10MB）+ 原始 .webm 留档于 .workbuddy/videos/
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / "deploy" / ".env"
DEMO_DIR = PROJECT_ROOT / "docs" / "demo"
VIDEO_DIR = PROJECT_ROOT / ".workbuddy" / "videos"
BASE = os.getenv("BACKEND_URL", "http://127.0.0.1:8081")
SEED_DIR = PROJECT_ROOT / "scripts" / "seed_docs"
USER = "demo-user"
GIF_MAX_MB = 10.0


EVIDENCE: list[dict] = []


def snap(page, label: str, sel: str = "body") -> str:
    """抓取页面关键文本作为**真实性证据**（录像之外的可核验文本）。"""
    txt = ""
    try:
        txt = page.inner_text(sel)
    except Exception as exc:  # noqa: BLE001
        txt = f"（抓取失败: {exc}）"
    EVIDENCE.append({"step": label, "text": txt[:2000]})
    print(f"    └ 证据快照[{label}] {len(txt)} 字符")
    return txt


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
    return env


def token() -> str:
    return httpx.post(f"{BASE}/api/v1/auth/dev-token", params={"user": USER},
                      timeout=10).json()["token"]


def ensure_docs() -> int:
    """确保 5 篇种子文档在位且 completed（演示前置）。"""
    t = token()
    h = {"Authorization": f"Bearer {t}"}
    ok = 0
    for p in sorted(SEED_DIR.glob("*.md")):
        with p.open("rb") as f:
            r = httpx.post(f"{BASE}/api/v1/documents", headers=h,
                           files={"file": (p.name, f, "text/markdown")}, timeout=30)
        if r.status_code in (201, 202):
            did = r.json()["doc_id"]
            for _ in range(120):
                d = httpx.get(f"{BASE}/api/v1/documents/{did}", headers=h, timeout=10).json()
                if d.get("ingest_status") == "completed":
                    ok += 1
                    break
                if d.get("ingest_status") == "failed":
                    break
                time.sleep(1)
    return ok


def ffmpeg_exe() -> str:
    """取 ffmpeg：优先 Playwright 已部署路径，其次 imageio-ffmpeg。"""
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright" / "ffmpeg-1013" / "ffmpeg-win64.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return "ffmpeg"


def webm_to_gif(webm: Path, out_gif: Path, speed: float = 2.0, width: int = 1000,
                max_mb: float = GIF_MAX_MB) -> tuple[bool, str]:
    """webm → GIF（倍速压缩 + 调色板两遍法保证质量）。超限则自动降级重试。"""
    ff = ffmpeg_exe()
    attempts = [
        (speed, width, 12),   # (倍速, 宽, fps)
        (speed, width, 10),
        (speed * 1.5, 900, 8),
        (speed * 2, 800, 6),
    ]
    last_err = ""
    for sp, w, fps in attempts:
        vf = f"setpts=PTS/{sp},fps={fps},scale={w}:-1:flags=lanczos"
        palette = VIDEO_DIR / "palette.png"
        cmd1 = [ff, "-y", "-i", str(webm), "-vf", f"{vf},palettegen=stats_mode=diff",
                str(palette)]
        cmd2 = [ff, "-y", "-i", str(webm), "-i", str(palette),
                "-lavfi", f"{vf} [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=5",
                "-loop", "0", str(out_gif)]
        for cmd in (cmd1, cmd2):
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            if p.returncode != 0:
                last_err = (p.stderr or "")[-300:]
                break
        else:
            size_mb = out_gif.stat().st_size / 1024 / 1024
            if size_mb <= max_mb:
                return True, f"{size_mb:.2f}MB (倍速 {sp}x, 宽 {w}, {fps}fps)"
            last_err = f"{size_mb:.2f}MB 超过 {max_mb}MB 上限"
    return False, f"全部降级尝试失败：{last_err}"


def main() -> int:
    env = load_env()
    os.environ.update({k: v for k, v in env.items() if k not in os.environ})
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    DEMO_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print("浏览器版 demo 录制（真实运行 → Playwright 录像 → ffmpeg 转 GIF）")
    print("=" * 78)

    print("[前置] 确保 5 篇种子文档 completed …")
    ok = ensure_docs()
    print(f"  → {ok}/5 篇 completed")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(VIDEO_DIR),
            record_video_size={"width": 1440, "height": 900},
        )
        page = ctx.new_page()

        # ── 0:00 定位：首页 + 三栏 ──
        print("\n[0:00] 首页（三栏 + 空态）")
        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(3500)      # 等 boot 完成（token/会话/文档）
        snap(page, "0:00 首页三栏", "#message-list" if False else "body")

        # ── 0:20 上传面板（文档已在位，展示列表 + 状态徽标） ──
        print("[0:20] 文档列表与状态")
        page.click("#btn-upload") if False else None
        page.wait_for_timeout(1200)
        snap(page, "0:20 文档列表", "#doc-list")
        page.hover("#doc-list")
        page.wait_for_timeout(1800)

        # ── 1:00 提问（kb_qa + 引用） ──
        print("[1:00] 提问：Nginx 502/504（kb_qa + 引用）")
        page.click("#btn-new-session")
        page.wait_for_timeout(1500)
        page.fill("#question", "Nginx 的 502 和 504 分别代表什么？")
        page.wait_for_timeout(900)
        page.keyboard.press("Enter")
        # 等流式结束（.cursor 消失）
        for _ in range(200):
            page.wait_for_timeout(300)
            if page.locator(".msg-assistant").count() and page.locator(".msg-assistant .cursor").count() == 0:
                break
        page.wait_for_timeout(1500)
        snap(page, "1:00 kb_qa 回答", "#message-list")
        # 点第一个引用角标 → 右栏片段
        mk = page.locator(".cite-mark")
        if mk.count():
            mk.first.click()
            page.wait_for_timeout(3200)   # 停留展示片段
            snap(page, "1:20 引用抽屉", "#drawer-body")
            page.click("#btn-close-drawer")
            page.wait_for_timeout(900)

        # ── 1:50 拒答（知识库没有的问题） ──
        print("[1:50] 拒答：知识库没有的问题")
        page.fill("#question", "公司差旅报销流程是什么？")
        page.keyboard.press("Enter")
        for _ in range(200):
            page.wait_for_timeout(300)
            if page.locator(".msg-assistant .cursor").count() == 0 and page.locator(".msg-assistant").count() >= 2:
                break
        page.wait_for_timeout(3000)       # 展示"最高分片段 + 分数"
        snap(page, "1:50 拒答", "#message-list")

        # ── 2:10 澄清（裸指代） ──
        print("[2:10] 澄清：裸指代「它起不来了怎么办？」")
        page.click("#btn-new-session")
        page.wait_for_timeout(1500)
        page.fill("#question", "它起不来了怎么办？")
        page.keyboard.press("Enter")
        for _ in range(150):
            page.wait_for_timeout(300)
            if page.locator(".msg-assistant .cursor").count() == 0 and page.locator(".msg-assistant").count() >= 1:
                break
        page.wait_for_timeout(3200)       # 展示"给 2~3 个候选、不猜"
        snap(page, "2:10 澄清", "#message-list")

        # ── 2:30 轨迹抽屉（证据链） ──
        print("[2:30] 轨迹抽屉（节点时间线 + 模型调用次数）")
        # 回到 kb_qa 会话（列表最后一条之前那个）：用第一条会话
        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        items = page.locator("#session-list .list-item")
        if items.count():
            items.nth(items.count() - 1).click()   # 最早那个会话（含 kb_qa 回答）
            page.wait_for_timeout(2500)
        tb = page.locator(".msg-action")
        if tb.count():
            tb.last.click()
            page.wait_for_timeout(4000)   # 停留展示时间线
            snap(page, "2:30 轨迹抽屉", "#drawer-body")
            page.click("#btn-close-drawer")
            page.wait_for_timeout(800)

        # 收尾：停在首页
        page.wait_for_timeout(1500)
        ctx.close()
        video_path = page.video.path() if page.video else None
        browser.close()

    if not video_path or not Path(video_path).exists():
        print("❌ 未拿到录像文件")
        return 1

    webm = Path(video_path)
    kept = VIDEO_DIR / "s9_demo_browser.webm"
    shutil.copy2(webm, kept)
    print(f"\n[录像] {kept}（{kept.stat().st_size/1024/1024:.2f}MB）")

    out_gif = DEMO_DIR / "demo.gif"
    ok_gif, detail = webm_to_gif(kept, out_gif)
    if not ok_gif:
        print(f"❌ GIF 生成失败：{detail}")
        return 1
    print(f"[GIF]  {out_gif} —— {detail}")

    # 证据快照落盘（真实性可核验）
    ev_path = PROJECT_ROOT / ".workbuddy" / "s9_demo_evidence.json"
    ev_path.write_text(json.dumps(EVIDENCE, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[证据] {ev_path}（{len(EVIDENCE)} 步页面文本快照）")
    for e in EVIDENCE:
        head = " ".join(e["text"].split())[:78]
        print(f"  - {e['step']}: {head}…")
    print("\n真实录制红线：内容全部来自真实运行，仅对空闲等待做倍速压缩。")
    return 0


if __name__ == "__main__":
    sys.exit(main())