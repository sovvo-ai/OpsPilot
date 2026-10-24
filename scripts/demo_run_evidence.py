"""3 分钟 demo 按 `docs/demo/demo-script.md` 实跑存证（逐段截图 + 文本证据）。

**与 `s9_demo_record.py` 的分工**：
  - `s9_demo_record.py` → 产出**演示 GIF**（给人看）；
  - 本脚本 → 产出**验收存证**（给审查看）：按分镜**逐段**截图到
    `docs/testing/screens/demo-run/`，并把每段的页面文本/接口输出落 JSON。

**红线**：全部真实运行，不摆拍。

用法：
    ai-service/.venv/Scripts/python.exe scripts/demo_run_evidence.py
产出：
    docs/testing/screens/demo-run/*.png   逐段截图（按时间码命名）
    .workbuddy/demo_run_evidence.json     逐段文本证据
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / "deploy" / ".env"
OUT_DIR = PROJECT_ROOT / "docs" / "testing" / "screens" / "demo-run"
BASE = os.getenv("BACKEND_URL", "http://127.0.0.1:8081")
SEED_DIR = PROJECT_ROOT / "scripts" / "seed_docs"
USER = "demo-user"

EVIDENCE: list[dict] = []


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


def wait_stream_done(page, timeout_s: float = 90.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        page.wait_for_timeout(300)
        if page.locator(".msg-assistant").count() and \
                page.locator(".msg-assistant .cursor").count() == 0:
            page.wait_for_timeout(600)
            if page.locator(".msg-assistant .cursor").count() == 0:
                return True
    return False


def snap(page, tcode: str, label: str, sel: str = "body") -> None:
    """按时间码截图 + 抓文本证据。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{tcode.replace(':', '')}_{label}"
    try:
        page.screenshot(path=str(OUT_DIR / f"{name}.png"), full_page=False)
        shot = f"docs/testing/screens/demo-run/{name}.png"
    except Exception as exc:  # noqa: BLE001
        shot = f"（截图失败：{exc}）"
    try:
        txt = page.inner_text(sel)
    except Exception as exc:  # noqa: BLE001
        txt = f"（抓取失败：{exc}）"
    EVIDENCE.append({"t": tcode, "step": label, "screenshot": shot, "text": txt[:2500]})
    print(f"    [{tcode}] {label}  → {shot}")


def main() -> int:
    env = load_env()
    os.environ.update({k: v for k, v in env.items() if k not in os.environ})

    print("=" * 78)
    print("3 分钟 demo 实跑存证（按 docs/demo/demo-script.md 分镜）")
    print("=" * 78)

    print("[前置] 5 篇种子文档 …")
    ok = ensure_docs()
    print(f"  → {ok}/5 篇 completed")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        # ── 0:00–0:20 定位：首页三栏 ──
        print("\n[0:00–0:20] 定位：首页三栏 + 架构")
        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(3500)
        snap(page, "0:00", "定位_首页三栏")

        # ── 0:20–1:00 上传 → 切片 → 引用可展开 ──
        print("\n[0:20–1:00] 上传与检索")
        snap(page, "0:20", "文档与摄取状态", "#doc-list")
        page.click("#btn-new-session")
        page.wait_for_timeout(1500)
        page.fill("#question", "Nginx 的 502 和 504 分别代表什么？")
        page.wait_for_timeout(700)
        page.keyboard.press("Enter")
        wait_stream_done(page)
        snap(page, "1:00", "kb_qa回答与引用", "#message-list")
        mk = page.locator(".cite-mark")
        if mk.count():
            mk.first.click()
            page.wait_for_timeout(2500)
            snap(page, "1:10", "引用抽屉_原文片段", "#drawer-body")
            page.click("#btn-close-drawer")
            page.wait_for_timeout(600)

        # ── 1:00–1:50 带日志排查 → 工具被调用 ──
        print("\n[1:00–1:50] 带日志排查（工具真的被调用）")
        page.fill("#question", "帮我看看这段日志")
        page.check("#chk-attach")
        page.wait_for_timeout(400)
        page.fill("#attach-log",
                  "2026-10-23 10:00:01 ERROR dial tcp 10.0.0.1:3306: connect: connection refused\n"
                  "2026-10-23 10:00:02 ERROR failed to connect to mysql: connection refused\n"
                  "2026-10-23 10:00:03 ERROR retry exhausted, exiting with code 1")
        page.wait_for_timeout(600)
        snap(page, "1:20", "附日志提问_输入态")
        page.keyboard.press("Enter")
        page.wait_for_timeout(1500)
        page.keyboard.press("Enter") if False else None
        # 点发送按钮（textarea 有内容时 Enter 已提交；这里兜底）
        if page.locator("#btn-send").is_enabled():
            try:
                page.click("#btn-send")
            except Exception:  # noqa: BLE001
                pass
        wait_stream_done(page)
        snap(page, "1:30", "排障回答")
        tb = page.locator(".msg-action")
        if tb.count():
            tb.last.click()
            page.wait_for_timeout(3000)
            snap(page, "1:40", "轨迹_工具调用", "#drawer-body")
            page.click("#btn-close-drawer")
            page.wait_for_timeout(600)

        # ── 1:50–2:30 拒答与澄清 ──
        print("\n[1:50–2:30] 拒答与澄清")
        # 先确保抽屉已关（上一段的轨迹抽屉若未关，后面截图会带旧内容）
        try:
            if page.is_visible("#drawer"):
                page.click("#btn-close-drawer")
                page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            pass
        page.click("#btn-new-session")
        page.wait_for_timeout(1800)
        page.fill("#question", "公司差旅报销流程是什么？")
        page.keyboard.press("Enter")
        wait_stream_done(page)
        wait_stream_done(page)
        snap(page, "2:00", "拒答_含最高分片段", "#message-list")

        # 澄清：**新会话**（无上下文）+ **必须清除 attach_log**。
        # ⚠️ 实测教训：前面排障段勾了"附日志"并填了内容，若不清除，澄清段提问会
        # **带着日志** → 按 D1 语义正确地走排障（不是缺陷，是本脚本漏清状态）。
        page.click("#btn-new-session")
        page.wait_for_timeout(2000)
        try:
            if page.is_visible("#attach-log"):
                if page.locator("#btn-clear-attach").count():
                    page.click("#btn-clear-attach")
                    page.wait_for_timeout(400)
        except Exception:  # noqa: BLE001
            pass
        page.fill("#question", "它起不来了怎么办？")
        page.wait_for_timeout(500)
        page.keyboard.press("Enter")
        # 澄清路径**零模型调用**（router 硬否决 → smalltalk），没有 delta 事件；
        # `wait_stream_done` 会立即返回，但前端 finally 的重渲染可能还没落地 →
        # 改为**等待助手消息非空**（可观测事实）。
        got = False
        for _ in range(60):
            page.wait_for_timeout(300)
            txt = page.inner_text("#message-list")
            if "信息不足" in txt or "请补充" in txt:
                got = True
                break
        print(f"    （澄清文案出现={got}）")
        snap(page, "2:20", "澄清_不猜", "#message-list")

        # ── 2:30–3:00 证据链 ──
        print("\n[2:30–3:00] 证据链与评测")
        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        items = page.locator("#session-list .list-item")
        if items.count():
            items.nth(items.count() - 1).click()
            page.wait_for_timeout(2500)
        tb2 = page.locator(".msg-action")
        if tb2.count():
            tb2.last.click()
            page.wait_for_timeout(3500)
            snap(page, "2:40", "轨迹_证据链", "#drawer-body")
            page.click("#btn-close-drawer")
            page.wait_for_timeout(600)
        try:
            if page.is_visible("#drawer"):
                page.click("#btn-close-drawer")
                page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            pass
        snap(page, "3:00", "收尾_全屏")

        ctx.close()
        browser.close()

    out = PROJECT_ROOT / ".workbuddy" / "demo_run_evidence.json"
    out.write_text(json.dumps(EVIDENCE, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n存证已落盘: {out}（{len(EVIDENCE)} 段）")
    for e in EVIDENCE:
        head = " ".join(e["text"].split())[:64]
        print(f"  {e['t']} {e['step']}: {head}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())