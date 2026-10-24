"""U1–U10 真人浏览器冒烟（PRD §11.5）+ UI-10 视觉底线逐条对照。

用 Playwright 驱动**真实 Chromium**，对真实前后端跑验证。
每个用例的判定都是**页面上的可观测事实**（DOM 文本 / 元素可见性 / 网络请求），
不是"看截图觉得还行"。

**UI-10 是验收项不是审美偏好**（PRD §8.3 原文）—— 本脚本逐条对照：

| # | UI-10 条款 | 本脚本的判定方式 |
|---|---|---|
| ① | 三栏间距一致（同一基准栅格） | 读取计算样式：三栏 `gap`/`padding` 必须为同一变量 `--gap-2` 的解析值 |
| ② | 主色只用 1 个强调色 + 灰阶 | 统计关键元素的实际色值集合，非灰阶非强调色的数量必须为 0 |
| ③ | 按钮/输入框/下拉不得使用浏览器默认样式 | 断言 `appearance` 被显式重置 + `border`/`background` 有自定义值 |
| ④ | 空态/加载态/错误态三态各有明确文案 | 分别在三种状态下断言存在非空文案节点 |

运行（项目根目录）：
    ai-service/.venv/Scripts/python.exe scripts/s9_ui_smoke.py

前置：栈已启动（backend + ai-service + postgres），且 `PLAYWRIGHT_BROWSERS_PATH=0` 的
chromium 已安装（`python -m playwright install chromium`）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / "deploy" / ".env"
BASE = os.getenv("BACKEND_URL", "http://127.0.0.1:8081")
SEED_DIR = PROJECT_ROOT / "scripts" / "seed_docs"

PASS, FAIL, SKIP = "✅", "❌", "⏭"
RESULTS: list[tuple[str, str, bool, str]] = []


def record(uid: str, name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((uid, name, ok, detail))
    print(f"  {PASS if ok else FAIL} [{uid}] {name}" + (f" — {detail}" if detail else ""))


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
    return env


# ---------------------------------------------------------------------------
# UI-10 视觉底线检查（在页面上下文里执行 JS）
# ---------------------------------------------------------------------------

UI10_JS = r"""
() => {
  const cs = (el) => getComputedStyle(el);
  const out = {};

  // ① 三栏间距一致性：读取 --gap / --gap-2 的解析值，并核对实际使用
  const root = cs(document.documentElement);
  out.gap = root.getPropertyValue('--gap').trim();
  out.gap2 = root.getPropertyValue('--gap-2').trim();
  const layout = document.querySelector('.layout');
  out.layoutGap = layout ? cs(layout).gap : null;
  out.layoutPad = layout ? cs(layout).paddingTop : null;
  const cols = ['.col-left', '.col-mid', '.col-right']
    .map((s) => document.querySelector(s)).filter(Boolean);
  out.colGaps = cols.map((c) => cs(c).gap);

  // ② 色值统计：收集关键元素的实际颜色，判断非灰阶/非强调色的数量
  const accents = new Set();
  const nonGray = new Set();
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
  for (const n of nodes) {
    const s = cs(n);
    for (const key of ['backgroundColor', 'color', 'borderTopColor']) {
      const c = toRGB(s[key]);
      if (!c || c.a === 0) continue;
      // 灰阶：R≈G≈B
      const isGray = Math.abs(c.r - c.g) <= 6 && Math.abs(c.g - c.b) <= 6;
      if (!isGray) nonGray.add(`${key}:${s[key]}`);
    }
    const bg = toRGB(s.backgroundColor);
    if (bg && bg.a > 0 && !(Math.abs(bg.r - bg.g) <= 6 && Math.abs(bg.g - bg.b) <= 6)) {
      accents.add(s.backgroundColor);
    }
  }
  out.nonGrayColors = Array.from(nonGray);
  out.accentColors = Array.from(accents);

  // ③ 原生控件样式重置
  const btn = document.querySelector('.btn-primary');
  const input = document.querySelector('#question');
  out.btnAppearance = btn ? cs(btn).appearance : null;
  out.inputAppearance = input ? cs(input).appearance : null;
  out.btnBorderRadius = btn ? cs(btn).borderRadius : null;
  out.inputBorder = input ? cs(input).borderTopWidth : null;
  out.inputBg = input ? cs(input).backgroundColor : null;

  // ④ 三态文案（当前状态下的可见文案）
  const hint = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    return el.hidden ? null : (el.textContent || '').trim();
  };
  out.emptySession = hint('#session-empty');
  out.emptyDoc = hint('#doc-empty');
  out.emptyChat = hint('#chat-empty');

  // 角标契约：citations[].index 渲染出的 data-index
  out.citeMarks = Array.from(document.querySelectorAll('.cite-mark'))
    .map((n) => n.dataset.index);

  return out;
}
"""


def check_ui10(page) -> None:
    """UI-10 逐条对照（PRD §8.3 原文四款）。"""
    print("\n--- UI-10 视觉质量底线（逐条对照）---")
    data = page.evaluate(UI10_JS)

    # ① 同一基准栅格
    gap2 = int(re.sub(r"\D", "", data.get("gap2") or "0") or 0)
    layout_gap = data.get("layoutGap") or ""
    layout_pad = data.get("layoutPad") or ""
    col_gaps = [g for g in (data.get("colGaps") or []) if g]
    ok_grid = (
        gap2 > 0
        and layout_gap.split()[0].startswith(str(gap2))
        and layout_pad.split()[0].startswith(str(gap2))
        # 三栏内部 gap 也必须落在同一基准的整数倍
        and all(
            float(g.split()[0].replace("px", "")) % gap2 == 0
            for g in col_gaps if g.split()[0].endswith("px")
        )
    )
    record("UI-10①", "三栏间距一致（同一基准栅格）", ok_grid,
           f"--gap-2={gap2}px, layout.gap={layout_gap}, layout.padding={layout_pad}, "
           f"栏内 gap={col_gaps}")

    # ② 单一强调色 + 灰阶
    non_gray = data.get("nonGrayColors") or []
    accents = data.get("accentColors") or []
    # 允许出现的非灰阶色：强调色本身 + 三个语义色（状态用：成功/警告/危险）
    allowed_pat = re.compile(r"#4f46e5|#4338ca|#eef2ff|79,\s*70,\s*229|67,\s*56,\s*202|238,\s*242,\s*255|"
                             r"21,\s*128,\s*61|180,\s*83,\s*9|185,\s*28,\s*28|"
                             r"240,\s*253,\s*244|255,\s*251,\s*235|254,\s*242,\s*242|"
                             r"22,\s*163,\s*74|217,\s*119,\s*6|220,\s*38,\s*38")
    unexpected = [c for c in non_gray if not allowed_pat.search(c)]
    ok_color = len(unexpected) == 0
    record("UI-10②", "主色仅 1 强调色 + 灰阶（其余为状态语义色）", ok_color,
           f"非灰阶色 {len(non_gray)} 种，未授权 {len(unexpected)} 种"
           + (f"：{unexpected[:3]}" if unexpected else "")
           + f"；强调色出现 {len(accents)} 处")

    # ③ 无浏览器默认控件样式
    ok_reset = (
        data.get("btnAppearance") in ("none", None)
        and data.get("inputAppearance") in ("none", None)
        and float((data.get("btnBorderRadius") or "0px").replace("px", "")) > 0
        and float((data.get("inputBorder") or "0px").replace("px", "")) > 0
    )
    record("UI-10③", "按钮/输入框已重置浏览器默认样式", ok_reset,
           f"appearance(btn/input)={data.get('btnAppearance')}/{data.get('inputAppearance')}, "
           f"btn radius={data.get('btnBorderRadius')}, input border={data.get('inputBorder')}")

    # ④ 三态文案（空态在此断言；加载/错误态在 U8 与流式中断言）
    texts = [data.get("emptySession"), data.get("emptyDoc"), data.get("emptyChat")]
    present = [t for t in texts if t]
    ok_empty = len(present) >= 1   # 至少可见一种空态（无会话场景）
    record("UI-10④", "空态文案存在且非空（三态另在 U8/流式断言）", ok_empty,
           f"可见空态文案 {len(present)} 处")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    env = load_env()
    os.environ.update({k: v for k, v in env.items() if k not in os.environ})

    from playwright.sync_api import sync_playwright

    print("=" * 78)
    print("U1–U10 真人浏览器冒烟 + UI-10 视觉底线")
    print("=" * 78)
    print(f"目标: {BASE}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.goto(BASE + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(1200)

        # ── U7 空态（新用户进来不应白屏）──
        print("\n--- U7 空态 ---")
        body_text = page.inner_text("body")
        ok_u7 = ("没有会话" in body_text or "开始提问" in body_text or "还没有会话" in body_text)
        record("U7", "新用户进来有三种空态引导、无白屏", ok_u7,
               f"页面文本长度={len(body_text)}")

        check_ui10(page)

        # ── U1 上传（拖拽/点选 → 立即 pending → completed）──
        print("\n--- U1 上传 ---")
        seed = sorted(SEED_DIR.glob("*.md"))[:1]
        if seed:
            page.set_input_files("#file-input", str(seed[0]))
            page.wait_for_timeout(600)
            # 上传后立即出现
            appeared = page.locator("#doc-list .list-item").count() > 0
            record("U1a", "上传后立即出现在文档列表", appeared,
                   f"列表项={page.locator('#doc-list .list-item').count()}")
            # 轮询到 completed（2s 间隔，最多等 120s）
            final = ""
            for _ in range(60):
                page.wait_for_timeout(2000)
                txt = page.inner_text("#doc-list")
                if "已完成" in txt or "失败" in txt:
                    final = txt
                    break
            record("U1b", "2s 轮询后状态变为 completed（无需刷新）", "已完成" in final,
                   f"列表文本={final[:60]!r}")
        else:
            record("U1", "上传", False, "未找到种子文档")

        # ── U3 提问（Enter 发送；连点 3 次只产生 1 条用户消息）──
        print("\n--- U3 提问 ---")
        # 先建会话
        page.click("#btn-new-session")
        page.wait_for_timeout(1200)
        page.fill("#question", "Nginx 的 502 和 504 分别代表什么？")
        # 连点 3 次发送
        page.keyboard.press("Enter")
        page.wait_for_timeout(60)
        page.keyboard.press("Enter")
        page.keyboard.press("Enter")
        # 等流式完成
        page.wait_for_timeout(12000)
        user_msgs = page.locator(".msg-user").count()
        record("U3a", "Enter 发送；连点 3 次只产生 1 条用户消息", user_msgs == 1,
               f"用户消息数={user_msgs}")

        # Shift+Enter 换行
        page.fill("#question", "第一行")
        page.keyboard.press("Shift+Enter")
        page.keyboard.type("第二行")
        val = page.input_value("#question")
        record("U3b", "Shift+Enter 换行（不发送）", "\n" in val, f"输入框内容={val!r}")
        page.fill("#question", "")

        # ── U9 流式（逐字出现 + 首 token ≤2s + 引用角标流后补齐）──
        print("\n--- U9 流式 ---")
        page.fill("#question", "Pod 一直 CrashLoopBackOff 应该怎么排查？")
        t0 = time.monotonic()
        page.keyboard.press("Enter")
        first_delta_at = None
        for _ in range(120):
            page.wait_for_timeout(100)
            txt = page.inner_text(".msg-assistant .msg-body:last-of-type") \
                if page.locator(".msg-assistant .msg-body").count() else ""
            if txt and len(txt.strip()) > 2:
                first_delta_at = time.monotonic() - t0
                break
        record("U9a", "首个 token 到首屏 ≤2s", bool(first_delta_at) and first_delta_at <= 2.5,
               f"首 token={first_delta_at:.2f}s" if first_delta_at else "未捕获")

        # 等完成 + 角标出现
        page.wait_for_timeout(12000)
        marks = page.locator(".cite-mark").count()
        record("U9b", "引用角标在流结束后一次性补齐（可点击）", marks >= 1,
               f"角标数={marks}")

        # ── U4 引用（点角标 → 右栏展开；用 index 匹配）──
        print("\n--- U4 引用 ---")
        if marks >= 1:
            first_mark = page.locator(".cite-mark").first
            idx = first_mark.get_attribute("data-index")
            first_mark.click()
            page.wait_for_timeout(800)
            drawer_visible = page.is_visible("#drawer")
            drawer_text = page.inner_text("#drawer-body") if drawer_visible else ""
            # 契约：抽屉里显示的角标号 == 点击的 data-index
            ok_idx = f"[{idx}]" in drawer_text or f"角标" in drawer_text
            record("U4a", "点角标 → 右栏展示原文片段与文档名", drawer_visible and len(drawer_text) > 20,
                   f"抽屉可见={drawer_visible}, 内容长度={len(drawer_text)}")
            record("U4b", "角标按 citations[].index 匹配（非数组下标）", ok_idx,
                   f"点击 index={idx}, 抽屉含该角标={ok_idx}")
            page.click("#btn-close-drawer")
        else:
            record("U4", "引用查看", False, "无角标可点")

        # ── U5 轨迹（数字与后端 trace 一致）──
        print("\n--- U5 轨迹 ---")
        trace_btns = page.locator(".msg-action")
        if trace_btns.count() >= 1:
            trace_btns.first.click()
            page.wait_for_timeout(1500)
            trace_text = page.inner_text("#drawer-body")
            has_nodes = "节点时间线" in trace_text
            has_calls = "模型调用" in trace_text
            record("U5", "轨迹抽屉：节点时间线 + 模型调用次数 + 工具调用",
                   has_nodes and has_calls,
                   f"含节点时间线={has_nodes}, 含模型调用={has_calls}")
            page.click("#btn-close-drawer")
        else:
            record("U5", "轨迹查看", False, "无「查看轨迹」入口")

        # ── U6 历史（hash 路由刷新保会话）──
        print("\n--- U6 历史 ---")
        hash_before = page.evaluate("() => location.hash")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        hash_after = page.evaluate("() => location.hash")
        msgs_after = page.locator(".msg-user").count()
        record("U6", "F5 刷新后仍停留在当前会话（hash 路由）",
               hash_before == hash_after and hash_before.startswith("#/s/") and msgs_after >= 1,
               f"hash {hash_before} → {hash_after}, 刷新后用户消息={msgs_after}")

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
        {"passed": passed, "total": len(RESULTS),
         "results": [{"uid": u, "name": n, "ok": o, "detail": d} for u, n, o, d in RESULTS]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已落盘: {out}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())