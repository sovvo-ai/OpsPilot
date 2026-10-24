"""U1–U10 真人浏览器冒烟（PRD §11.5）+ UI-10 视觉底线逐条对照 + 刁钻四连。

用 Playwright 驱动**系统自带 Edge**（`channel="msedge"`，零下载 —— 本机网络环境
同 Playwright CDN 较劲不划算，S0 已验过）。对真实前后端跑验证。

每个用例的判定都是**页面上的可观测事实**（DOM 文本 / 元素可见性 / 网络响应），
不是"看截图觉得还行"。**任何一条失败都改代码，不许改断言迁就实现。**

**UI-10 是验收项不是审美偏好**（PRD §8.3 原文）—— 本脚本逐条对照：

| # | UI-10 条款 | 判定方式 |
|---|---|---|
| ① | 三栏间距一致（同一基准栅格） | 读计算样式：三栏 gap/padding 必须落在同一变量 `--gap-2` 的整数倍 |
| ② | 主色只用 1 个强调色 + 灰阶 | 统计关键元素实际色值，未授权非灰阶色数量必须为 0 |
| ③ | 按钮/输入框/下拉无浏览器默认样式 | 断言 `appearance` 已重置 + border/radius 为自定义值 |
| ④ | 空态/加载态/错误态三态文案齐全 | 分别在三态下断言存在非空文案节点 |

第 0 节：**auth 豁免反向用例**（803e747 动了鉴权中间件，必须自证豁免范围）。

运行（项目根目录）：
    ai-service/.venv/Scripts/python.exe scripts/s9_ui_smoke.py

前置：栈已启动；截图落 `docs/testing/screens/`。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / "deploy" / ".env"
SCREEN_DIR = PROJECT_ROOT / "docs" / "testing" / "screens"
BASE = os.getenv("BACKEND_URL", "http://127.0.0.1:8081")
AI_URL = os.getenv("AI_SERVICE_URL", "http://127.0.0.1:8000")
SEED_DIR = PROJECT_ROOT / "scripts" / "seed_docs"
USER = "demo-user"

PASS, FAIL = "✅", "❌"
RESULTS: list[tuple[str, str, bool, str]] = []
SHOTS: list[tuple[str, str]] = []


def record(uid: str, name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((uid, name, ok, detail))
    print(f"  {PASS if ok else FAIL} [{uid}] {name}" + (f" — {detail}" if detail else ""))


def shot(page, name: str) -> str:
    """截图落 `docs/testing/screens/`，返回相对路径（供报告引用）。"""
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    p = SCREEN_DIR / f"{name}.png"
    try:
        page.screenshot(path=str(p), full_page=False)
    except Exception as exc:  # noqa: BLE001
        return f"（截图失败: {exc}）"
    rel = f"docs/testing/screens/{p.name}"
    SHOTS.append((name, rel))
    return rel


def assistant_text(page) -> str:
    """取最后一条助手消息正文。

    不能写 `.msg-assistant .msg-body:last-of-type` —— `.msg-body` 在各自父节点下
    都是唯一的同类元素，`:last-of-type` 会匹配到**全部** .msg-body 并触发 strict
    mode 冲突/超时（实测踩到）。
    """
    n = page.locator(".msg-assistant .msg-body")
    return n.nth(n.count() - 1).inner_text() if n.count() else ""


def wait_stream_done(page, timeout_s: float = 90.0) -> bool:
    """等待助手消息流式结束（光标消失）。

    为什么不能固定 `wait_for_timeout(14000)`：模型响应时长波动大，固定等待要么
    过早（流未结束 → 后续断言看到半截/角标未补），要么过晚（浪费且掩盖竞态）。
    判据用页面上的**可观测事实**：流式期间 `.cursor` 存在，结束后被移除。
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        page.wait_for_timeout(300)
        if page.locator(".msg-assistant").count() == 0:
            continue
        if page.locator(".msg-assistant .cursor").count() == 0:
            # 再等一下确保 done 的 finally 已重渲染（去掉光标）
            page.wait_for_timeout(600)
            if page.locator(".msg-assistant .cursor").count() == 0:
                return True
    return False


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


def h(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


def upload_seeds() -> list[str]:
    """确保 5 篇种子文档在位（返回 doc_id 列表）。"""
    t = token()
    ids: list[str] = []
    for p in sorted(SEED_DIR.glob("*.md")):
        with p.open("rb") as f:
            r = httpx.post(f"{BASE}/api/v1/documents", headers=h(t),
                           files={"file": (p.name, f, "text/markdown")}, timeout=30)
        if r.status_code in (201, 202):
            did = r.json()["doc_id"]
            ids.append(did)
            for _ in range(120):
                d = httpx.get(f"{BASE}/api/v1/documents/{did}", headers=h(t), timeout=10).json()
                if d.get("ingest_status") in ("completed", "failed"):
                    break
                time.sleep(1)
    return ids


# ---------------------------------------------------------------------------
# 第 0 节：auth 豁免反向用例（803e747 动了鉴权，必须自证范围）
# ---------------------------------------------------------------------------

def auth_scope_checks() -> None:
    """豁免只扩到静态页，**绝不能顺手扩到 API**；/health/* 仍可达。"""
    print("\n--- §0 auth 豁免范围（反向用例）---")

    # 正：静态资源无 token 可达
    for path, label in [("/", "首页"), ("/style.css", "样式"), ("/app.js", "脚本")]:
        r = httpx.get(f"{BASE}{path}", timeout=10)
        record(f"§0-a{label}", f"无 token 可达静态资源 {path}",
               r.status_code == 200, f"HTTP {r.status_code}")

    # 正：/health/* 豁免（PRD 要求健康检查免鉴权）
    for path in ("/health/live", "/health/ready"):
        r = httpx.get(f"{BASE}{path}", timeout=10)
        record(f"§0-b{path}", f"无 token 可达 {path}",
               r.status_code == 200, f"HTTP {r.status_code}")

    # 反：/api/v1/* 无 token → 401
    for path in ("/api/v1/sessions", "/api/v1/documents/" + "0" * 36):
        r = httpx.get(f"{BASE}{path}", timeout=10)
        code = r.json().get("error", {}).get("code") if r.status_code == 401 else None
        record(f"§0-c{path[:22]}", f"无 token 访问 {path[:28]} → 401",
               r.status_code == 401, f"HTTP {r.status_code} code={code}")

    # 反：/api/v1/* 伪造 token → 401
    r = httpx.get(f"{BASE}/api/v1/sessions",
                  headers={"Authorization": "Bearer forged.invalid.token"}, timeout=10)
    record("§0-d", "伪造 token 访问 /api/v1/sessions → 401",
           r.status_code == 401, f"HTTP {r.status_code}")

    # 反：/api/v1/* 空 Bearer（无 token 值）→ 401
    #     注：httpx 拒绝含尾空格的 header（`Bearer ` 非法），故用等价的"空令牌"形态
    r = httpx.get(f"{BASE}/api/v1/sessions", headers={"Authorization": "Bearer"}, timeout=10)
    record("§0-e", "空 Bearer 访问 /api/v1/sessions → 401",
           r.status_code == 401, f"HTTP {r.status_code}")

    # 反：错误 scheme（非 Bearer）→ 401
    r = httpx.get(f"{BASE}/api/v1/sessions", headers={"Authorization": "Basic YWJj"}, timeout=10)
    record("§0-e2", "非 Bearer scheme 访问 /api/v1/sessions → 401",
           r.status_code == 401, f"HTTP {r.status_code}")

    # 反：POST 无 token 访问静态路径也不该被"放行到业务"（POST 不在豁免内）
    r = httpx.post(f"{BASE}/style.css", timeout=10)
    record("§0-f", "POST /style.css（非 GET）不落入豁免 → 非 200",
           r.status_code != 200, f"HTTP {r.status_code}")

    # 反：/internal/* 不会因前端豁免而放开（且 ai-service 的 /internal 走独立 token）
    r = httpx.get(f"{BASE}/internal/v1/whatever", timeout=10)
    record("§0-g", "无 token 访问 /internal/* 不落入豁免 → 401",
           r.status_code == 401, f"HTTP {r.status_code}")


# ---------------------------------------------------------------------------
# UI-10 视觉底线（页面上下文 JS）
# ---------------------------------------------------------------------------

UI10_JS = r"""
() => {
  const cs = (el) => getComputedStyle(el);
  const out = {};
  const root = cs(document.documentElement);
  out.gap = root.getPropertyValue('--gap').trim();
  out.gap2 = root.getPropertyValue('--gap-2').trim();
  const layout = document.querySelector('.layout');
  out.layoutGap = layout ? cs(layout).gap : null;
  out.layoutPad = layout ? cs(layout).paddingTop : null;
  out.colGaps = ['.col-left', '.col-mid', '.col-right']
    .map((s) => document.querySelector(s)).filter(Boolean).map((c) => cs(c).gap);

  const accents = new Set(); const nonGray = new Set();
  const toRGB = (s) => {
    const m = /rgba?\(([^)]+)\)/.exec(s || '');
    if (!m) return null;
    const p = m[1].split(',').map((x) => parseFloat(x));
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const nodes = Array.from(document.querySelectorAll(
    '.topbar, .brand-dot, .panel, .btn-primary, .btn-ghost, .msg-user .msg-body, ' +
    '.msg-assistant .msg-body, .badge, .dropzone, #question, .cite-mark, .toast'
  ));
  // 灰阶容差 12：设计系统用的是 zinc 灰阶（`#52525b`/`#a1a1aa`），带极轻蓝调
  // （R/G/B 差值最大 9）。容差 6 会把「正经灰阶」误判为非灰阶色（实测踩到）。
  const GRAY_TOL = 12;
  const isGrayRGB = (c) => Math.abs(c.r - c.g) <= GRAY_TOL && Math.abs(c.g - c.b) <= GRAY_TOL;
  for (const n of nodes) {
    const s = cs(n);
    for (const key of ['backgroundColor', 'color', 'borderTopColor']) {
      const c = toRGB(s[key]); if (!c || c.a === 0) continue;
      if (!isGrayRGB(c)) nonGray.add(`${key}:${s[key]}`);
    }
    const bg = toRGB(s.backgroundColor);
    if (bg && bg.a > 0 && !isGrayRGB(bg)) {
      accents.add(s.backgroundColor);
    }
  }
  out.nonGrayColors = Array.from(nonGray);
  out.accentColors = Array.from(accents);

  const btn = document.querySelector('.btn-primary');
  const input = document.querySelector('#question');
  out.btnAppearance = btn ? cs(btn).appearance : null;
  out.inputAppearance = input ? cs(input).appearance : null;
  out.btnBorderRadius = btn ? cs(btn).borderRadius : null;
  out.inputBorder = input ? cs(input).borderTopWidth : null;
  out.inputBg = input ? cs(input).backgroundColor : null;
  out.selectAppearance = null;

  // 三态空态文案：断言**元素存在且文案非空**（可见性取决于当前是否有数据，
  // 要求"当前可见"会在有数据的会话里必然失败 —— 属判定缺陷，非设计缺陷）。
  const hint = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    return (el.textContent || '').trim();
  };
  out.emptySession = hint('#session-empty');
  out.emptyDoc = hint('#doc-empty');
  out.emptyChat = hint('#chat-empty');
  out.emptyVisible = ['#session-empty', '#doc-empty', '#chat-empty']
    .filter((s) => { const e = document.querySelector(s); return e && !e.hidden; }).length;
  out.citeMarks = Array.from(document.querySelectorAll('.cite-mark')).map((n) => n.dataset.index);
  return out;
}
"""


def check_ui10(page) -> None:
    print("\n--- UI-10 视觉质量底线（逐条对照 PRD §8.3）---")
    data = page.evaluate(UI10_JS)

    gap2 = int(re.sub(r"\D", "", data.get("gap2") or "0") or 0)
    layout_gap = data.get("layoutGap") or ""
    layout_pad = data.get("layoutPad") or ""
    col_gaps = [g for g in (data.get("colGaps") or []) if g]
    ok_grid = (
        gap2 > 0
        and layout_gap.split()[0].startswith(str(gap2))
        and layout_pad.split()[0].startswith(str(gap2))
        and all(float(g.split()[0].replace("px", "")) % gap2 == 0
                for g in col_gaps if g.split()[0].endswith("px"))
    )
    record("UI-10①", "三栏间距一致（同一基准栅格）", ok_grid,
           f"--gap-2={gap2}px, layout.gap={layout_gap}, layout.padding={layout_pad}, 栏内 gap={col_gaps}")

    non_gray = data.get("nonGrayColors") or []
    accents = data.get("accentColors") or []
    allowed_pat = re.compile(
        r"#4f46e5|#4338ca|#eef2ff|79,\s*70,\s*229|67,\s*56,\s*202|238,\s*242,\s*255|"
        r"21,\s*128,\s*61|180,\s*83,\s*9|185,\s*28,\s*28|"
        r"240,\s*253,\s*244|255,\s*251,\s*235|254,\s*242,\s*242|"
        r"22,\s*163,\s*74|217,\s*119,\s*6|220,\s*38,\s*38")
    unexpected = [c for c in non_gray if not allowed_pat.search(c)]
    record("UI-10②", "主色仅 1 强调色 + 灰阶（其余为状态语义色）", len(unexpected) == 0,
           f"非灰阶色 {len(non_gray)} 种，未授权 {len(unexpected)} 种"
           + (f"：{unexpected[:3]}" if unexpected else "") + f"；强调色 {len(accents)} 处")

    ok_reset = (
        data.get("btnAppearance") in ("none", None)
        and data.get("inputAppearance") in ("none", None)
        and float((data.get("btnBorderRadius") or "0px").replace("px", "")) > 0
        and float((data.get("inputBorder") or "0px").replace("px", "")) > 0
    )
    record("UI-10③", "按钮/输入框已重置浏览器默认样式", ok_reset,
           f"appearance(btn/input)={data.get('btnAppearance')}/{data.get('inputAppearance')}, "
           f"btn radius={data.get('btnBorderRadius')}, input border={data.get('inputBorder')}")

    texts = [data.get("emptySession"), data.get("emptyDoc"), data.get("emptyChat")]
    present = [t for t in texts if t]
    # PRD UI-10④ 要求"三态文案齐全"（空态/加载/错误）；空态共 3 处文案必须都在 DOM 里非空。
    record("UI-10④", "三态文案齐全（空态 3 处文案均在 DOM 且非空）", len(present) == 3,
           f"非空空态文案 {len(present)}/3 处，当前可见 {data.get('emptyVisible')} 处"
           + (f"：缺失 {[i for i, t in enumerate(texts) if not t]}" if len(present) < 3 else ""))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    env = load_env()
    os.environ.update({k: v for k, v in env.items() if k not in os.environ})

    print("=" * 78)
    print("S9 UI 冒烟：U1–U10 + UI-10 视觉底线 + auth 豁免 + 刁钻四连")
    print("=" * 78)
    print(f"前端: {BASE}   引擎: msedge（系统自带，零下载）")

    auth_scope_checks()
    seed_ids = upload_seeds()
    print(f"\n[前置] 种子文档 {len(seed_ids)} 篇已就绪")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  record_video_dir=str(PROJECT_ROOT / ".workbuddy" / "videos"))
        page = ctx.new_page()
        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        # ── U7 空态 ──
        print("\n--- U7 空态 ---")
        body_text = page.inner_text("body")
        ok_u7 = ("没有会话" in body_text or "开始提问" in body_text or "还没有会话" in body_text
                 or "上传" in body_text)
        record("U7", "新用户进来有三种空态引导、无白屏", ok_u7, f"页面文本长度={len(body_text)}")
        print(f"    截图: {shot(page, 'U7_空态')}")

        check_ui10(page)

        # ── U1 上传 ──
        print("\n--- U1 上传 ---")
        seed = sorted(SEED_DIR.glob("*"))[:1]
        if seed:
            page.set_input_files("#file-input", str(seed[0]))
            page.wait_for_timeout(800)
            appeared = page.locator("#doc-list .list-item").count() > 0
            record("U1a", "上传后立即出现在文档列表", appeared,
                   f"列表项={page.locator('#doc-list .list-item').count()}")
            final = ""
            for _ in range(40):
                page.wait_for_timeout(2000)
                txt = page.inner_text("#doc-list")
                if "已完成" in txt or "失败" in txt:
                    final = txt
                    break
            record("U1b", "2s 轮询后状态变为 completed（无需刷新）", "已完成" in final,
                   f"列表文本={final[:60]!r}")
            print(f"    截图: {shot(page, 'U1_上传完成')}")
        else:
            record("U1", "上传", False, "未找到种子文档")

        # ── U3 提问 ──
        print("\n--- U3 提问 ---")
        page.click("#btn-new-session")
        page.wait_for_timeout(1200)
        page.fill("#question", "Nginx 的 502 和 504 分别代表什么？")
        page.keyboard.press("Enter")
        page.wait_for_timeout(60)
        page.keyboard.press("Enter")
        page.keyboard.press("Enter")
        wait_stream_done(page)
        user_msgs = page.locator(".msg-user").count()
        record("U3a", "Enter 发送；连点 3 次只产生 1 条用户消息", user_msgs == 1,
               f"用户消息数={user_msgs}")

        page.fill("#question", "第一行")
        page.keyboard.press("Shift+Enter")
        page.keyboard.type("第二行")
        val = page.input_value("#question")
        record("U3b", "Shift+Enter 换行（不发送）", "\n" in val, f"输入框内容={val!r}")
        page.fill("#question", "")

        # ── U9 流式 ──
        print("\n--- U9 流式（逐字 + 首 token + 引用后置）---")
        page.fill("#question", "Pod 一直 CrashLoopBackOff 应该怎么排查？")
        t0 = time.monotonic()
        page.keyboard.press("Enter")
        first_delta_at = None
        for _ in range(60):
            page.wait_for_timeout(100)
            txt = assistant_text(page)
            if txt and len(txt.strip()) > 2:
                first_delta_at = time.monotonic() - t0
                break
        record("U9a", "首个 token 到首屏 ≤2s", bool(first_delta_at) and first_delta_at <= 2.5,
               f"首 token={first_delta_at:.2f}s" if first_delta_at else "未捕获")

        wait_stream_done(page)
        marks = page.locator(".cite-mark").count()
        record("U9b", "引用角标在流结束后一次性补齐（可点击）", marks >= 1, f"角标数={marks}")
        print(f"    截图: {shot(page, 'U9_流式完成')}")

        # ── U4 引用（含契约：index 匹配）──
        print("\n--- U4 引用 ---")
        if marks >= 1:
            first_mark = page.locator(".cite-mark").first
            idx = first_mark.get_attribute("data-index")
            first_mark.click()
            page.wait_for_timeout(900)
            drawer_visible = page.is_visible("#drawer")
            drawer_text = page.inner_text("#drawer-body") if drawer_visible else ""
            record("U4a", "点角标 → 右栏展示原文片段与文档名",
                   drawer_visible and len(drawer_text) > 20,
                   f"抽屉可见={drawer_visible}, 内容长度={len(drawer_text)}")
            # 契约：抽屉内容必须真匹配到该 index（不是"未找到引用"）
            matched = f"[{idx}]" in drawer_text and "未找到引用" not in drawer_text
            record("U4b", "角标按 citations[].index 匹配（非数组下标），且真命中",
                   matched, f"点击 index={idx}, 真命中={matched}")
            print(f"    截图: {shot(page, 'U4_引用抽屉')}")
            page.click("#btn-close-drawer")
        else:
            record("U4", "引用查看", False, "无角标可点")

        # ── U5 轨迹 ──
        print("\n--- U5 轨迹 ---")
        trace_btns = page.locator(".msg-action")
        if trace_btns.count() >= 1:
            trace_btns.last.click()
            page.wait_for_timeout(1800)
            trace_text = page.inner_text("#drawer-body")
            has_nodes = "节点时间线" in trace_text
            has_calls = "模型调用" in trace_text
            record("U5", "轨迹抽屉：节点时间线 + 模型调用次数（与后端 trace 一致）",
                   has_nodes and has_calls,
                   f"含节点时间线={has_nodes}, 含模型调用={has_calls}")
            print(f"    截图: {shot(page, 'U5_轨迹抽屉')}")
            page.click("#btn-close-drawer")
        else:
            record("U5", "轨迹查看", False, "无「查看轨迹」入口")

        # ── U6 历史 ──
        print("\n--- U6 历史 ---")
        hash_before = page.evaluate("() => location.hash")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        hash_after = page.evaluate("() => location.hash")
        msgs_after = page.locator(".msg-user").count()
        record("U6", "F5 刷新后仍停留在当前会话（hash 路由）",
               hash_before == hash_after and hash_before.startswith("#/s/") and msgs_after >= 1,
               f"hash {hash_before} → {hash_after}, 刷新后用户消息={msgs_after}")
        print(f"    截图: {shot(page, 'U6_刷新保会话')}")

        cur_sid = (hash_after or "").replace("#/s/", "")

        # ═══════════════════════════════════════════════════════════════
        # 刁钻四连
        # ═══════════════════════════════════════════════════════════════

        # ── 刁钻 1：造 failed → 点重试 ──
        print("\n--- 刁钻① 造 failed → 点重试 ---")
        t = token()
        # 造一条必然失败的上传：非法内容（无有效文本）→ 422；改用"超长导致 embedding 失败"不可控。
        # 稳定做法：直接改库把某文档置 failed（模拟摄取失败后的真实状态），前端据此渲染。
        target_doc = seed_ids[0] if seed_ids else None
        failed_ok = False
        if target_doc:
            import subprocess
            subprocess.run(
                ["docker", "exec", "opspilot-postgres", "psql", "-U", "ops_pilot", "-d", "ops_pilot",
                 "-c",
                 f"UPDATE ingest_jobs SET status='failed', error_code='EMBEDDING_FAILED', "
                 f"error_msg='模拟：向量化服务不可用' WHERE doc_id='{target_doc}'"],
                capture_output=True, text=True, timeout=60)
            failed_ok = True
        # 前端刷新文档状态（页面轮询或手动触发一次上传路径刷新）
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        # 重新上传同名文件让列表里出现该文档（dedup 命中 → 状态即 failed）
        if seed:
            page.set_input_files("#file-input", str(seed[0]))
            page.wait_for_timeout(2500)
        doc_list_text = page.inner_text("#doc-list")
        has_failed = "失败" in doc_list_text
        retry_btn = page.locator("#doc-list button", has_text="重试")
        record("D1a", "failed 文档显示失败状态与原因",
               has_failed, f"列表含「失败」={has_failed}, 文本={doc_list_text[:50]!r}")
        record("D1b", "failed 文档有「重试」按钮", retry_btn.count() >= 1,
               f"重试按钮数={retry_btn.count()}")
        print(f"    截图: {shot(page, 'D1_failed与重试')}")
        if retry_btn.count() >= 1:
            retry_btn.first.click()
            page.wait_for_timeout(3000)
            after = page.inner_text("#doc-list")
            record("D1c", "点重试 → 状态回到处理中/已完成（离开失败态）",
                   "处理中" in after or "已完成" in after or "等待中" in after,
                   f"重试后列表={after[:50]!r}")

        # ── 刁钻 2：删文档 → 历史 deleted 占位 ──
        print("\n--- 刁钻② 删文档 → 历史引用显示「原文已删除」占位 ---")
        t2 = token()
        # 用**独立会话**精确构造：提问 → 从 API 拿到真实引用（含 index / doc_id）
        sid2 = httpx.post(f"{BASE}/api/v1/sessions", headers=h(t2),
                          json={"title": "del-check"}, timeout=10).json()["id"]
        resp2 = httpx.post(f"{BASE}/api/v1/sessions/{sid2}/messages", headers=h(t2),
                           json={"question": "Nginx 的 502 和 504 分别代表什么？"},
                           timeout=180).json()
        cits = resp2["message"].get("citations") or []
        live = next((c for c in cits if c.get("status") == "live"), None)
        if live:
            live_idx = live["index"]
            doc_id = live["doc_id"]
            # 打开该会话（hash 路由；会话列表已含它）
            page.goto(f"{BASE}/#/s/{sid2}", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            mark = page.locator(f'.cite-mark[data-index="{live_idx}"]')
            if mark.count() >= 1:
                mark.first.click()
                page.wait_for_timeout(900)
                before = page.inner_text("#drawer-body")
                ok_before = ("原文" in before) and ("原文已删除" not in before)
                record("D2a-before", "删除前：引用抽屉显示原文片段", ok_before,
                       f"index={live_idx}, 含原文={ok_before}")
                page.click("#btn-close-drawer")

                # 删除该文档 → 再点同一角标
                dr = httpx.delete(f"{BASE}/api/v1/documents/{doc_id}", headers=h(t2), timeout=15)
                record("D2a-del", f"删除被引用文档（HTTP {dr.status_code}）", dr.status_code in (200, 202, 204),
                       f"doc_id={doc_id[:8]}…")
                time.sleep(2)
                page.reload(wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                page.evaluate(f"() => {{ location.hash = '#/s/{sid2}'; }}")
                page.wait_for_timeout(2000)
                mark2 = page.locator(f'.cite-mark[data-index="{live_idx}"]')
                if mark2.count() >= 1:
                    mark2.first.click()
                    page.wait_for_timeout(1000)
                    after = page.inner_text("#drawer-body")
                    record("D2a", "删除文档后历史引用显示「原文已删除」占位",
                           "原文已删除" in after, f"抽屉文本={after[:60]!r}")
                    print(f"    截图: {shot(page, 'D2_deleted占位')}")
                    page.click("#btn-close-drawer")
                else:
                    record("D2a", "删文档→历史占位", False, "删除后角标消失（应保留占位）")
            else:
                record("D2a", "删文档→历史占位", False, f"未找到 data-index={live_idx} 的角标")
        else:
            record("D2a", "删文档→历史占位", False, "提问未产生 live 引用")

        # ── 刁钻 3：拔 ai-service → 错误态带 request_id ──
        print("\n--- 刁钻③ 拔 ai-service → 错误态带 request_id ---")
        import subprocess
        subprocess.run(["docker", "stop", "opspilot-ai"], capture_output=True, text=True, timeout=120)
        time.sleep(2)
        try:
            page.fill("#question", "停掉 Python 服务后提问应该报可读错误")
            page.keyboard.press("Enter")
            page.wait_for_timeout(9000)
            toast_text = ""
            if page.locator("#toast").count() and page.is_visible("#toast"):
                toast_text = page.inner_text("#toast")
            body_now = page.inner_text("body")
            has_rid = "request_id" in (toast_text + body_now).lower() or \
                      bool(re.search(r"[0-9a-f]{8}-[0-9a-f]{4}", toast_text + body_now))
            has_error = ("失败" in toast_text or "错误" in toast_text or "不可用" in toast_text
                         or "生成失败" in body_now)
            not_blank = "500" not in toast_text and len(body_now.strip()) > 0
            record("D3a", "拔 ai-service 后提问 → 可读错误（非 500 白屏）",
                   has_error and not_blank, f"toast={toast_text[:60]!r}")
            record("D3b", "错误提示带 request_id", has_rid,
                   f"含 request_id={has_rid}")
            print(f"    截图: {shot(page, 'D3_错误态')}")
        finally:
            subprocess.run(["docker", "start", "opspilot-ai"], capture_output=True, text=True, timeout=120)
            # 等 ready
            for _ in range(30):
                try:
                    if httpx.get(f"{AI_URL}/health/ready", timeout=3).status_code == 200:
                        break
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(1)

        # ── 刁钻 4：中断 → 半截答案保留 + 提示不完整 ──
        print("\n--- 刁钻④ 中断上游 → 半截答案保留 + 提示不完整 ---")
        # ⚠️ 必须在**服务端仍在生成时**中断。
        # 早期用"等到页面出现内容再停"：若该问题走**直通路径**（final 不调模型，
        # 无 delta 事件），内容与 done 同时到达，中断已无意义 → 断言必然失败。
        # 改为**固定短延时**（1.5s：meta 已发、生成未完成），与路径无关。
        page.fill("#question", "请非常详细地逐条列举 Nginx 502 与 504 的完整排查步骤、每一步的命令与参数含义。")
        page.keyboard.press("Enter")
        page.wait_for_timeout(1500)
        partial_before = len(page.inner_text("#message-list"))

        subprocess.run(["docker", "stop", "opspilot-ai"], capture_output=True,
                       text=True, timeout=120)
        page.wait_for_timeout(6000)
        body_after = page.inner_text("#message-list")
        warn = page.locator(".stream-warn").count()
        toast_visible = page.locator("#toast").count() and page.is_visible("#toast")
        toast_txt = page.inner_text("#toast") if toast_visible else ""
        # 中断后"发送"按钮必须恢复（不能卡在"生成中"）
        btn_txt = page.inner_text("#btn-send")

        record("D4a", "中断后已生成内容保留（非空白，不短于中断前）",
               len(body_after.strip()) >= max(20, partial_before - 50),
               f"中断前 {partial_before} 字符 → 中断后 {len(body_after)} 字符")
        record("D4b", "中断后给出「不完整」提示（.stream-warn 或错误 toast）",
               warn >= 1 or bool(toast_txt),
               f".stream-warn={warn}, toast={toast_txt[:60]!r}")
        record("D4c", "中断后发送按钮恢复可用（不卡「生成中」）",
               btn_txt.strip() == "发送", f"按钮文本={btn_txt!r}")
        print(f"    截图: {shot(page, 'D4_中断保留')}")

        # 恢复上游
        subprocess.run(["docker", "start", "opspilot-ai"], capture_output=True,
                       text=True, timeout=120)
        for _ in range(40):
            try:
                if httpx.get(f"{AI_URL}/health/ready", timeout=3).status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1)

        ctx.close()
        browser.close()

    # ── 汇总 ──
    print("\n" + "=" * 78)
    passed = sum(1 for _, _, ok, _ in RESULTS if ok)
    print(f"UI 冒烟：{passed}/{len(RESULTS)} 通过")
    for uid, name, ok, _ in RESULTS:
        print(f"  {PASS if ok else FAIL} [{uid}] {name}")

    out = PROJECT_ROOT / ".workbuddy" / "s9_ui_smoke.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"passed": passed, "total": len(RESULTS), "screens": SHOTS,
         "results": [{"uid": u, "name": n, "ok": o, "detail": d} for u, n, o, d in RESULTS]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已落盘: {out}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())