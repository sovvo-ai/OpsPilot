"""router 节点：规则优先，LLM 兜底（agent-workflow §3.1 / PRD §7.1）。

判定顺序（命中即短路，**顺序即优先级**）：
  1. 输入非法（Go 侧已拦）
  2. smalltalk 强命中（问候/致谢/在吗/你是谁/能做什么）→ 零检索零生成
  3. out_of_scope（写代码/翻译/通用闲聊/与本系统无关）→ 零检索零生成
  4. troubleshooting 特征（堆栈/error/failed/错误码/异常类名/端口+"起不来"）
  5. 其余 → kb_qa（规则不确定时的默认档）

规则强命中记 `route_conf=1.0` 且 **LLM 调用数为 0**（A5 硬要求）；
规则不确定时才调 1 次模型（JSON mode），模型只能输出这 4 类之一。
"""

from __future__ import annotations

import json
import logging
import re

from app.graph.budget import PRIORITY_HIGH, Budget

logger = logging.getLogger("app.graph.router")

SMALLTALK_PATTERNS = (
    r"^你好[啊呀！!。]*$",
    r"^您好[啊呀！!。]*$",
    r"^hi\b",
    r"^hello\b",
    r"^在吗[？?]*$",
    r"^谢谢[你您]?[！!。]*$",
    r"^感谢",
    r"^你是谁",
    r"^你是什么",
    r"^你能做什么",
    r"^你会什么",
    r"^介绍一下你",
)

OUT_OF_SCOPE_PATTERNS = (
    r"写(一段|个|下)?(代码|程序|脚本|函数|算法)",
    r"帮我写",
    r"翻译",
    r"讲个笑话",
    r"今天天气",
    r"点个外卖",
    r"推荐(一部|个)?(电影|电视剧|歌)",
    r"做首诗",
    r"算一下.{0,10}等于几",
)

# troubleshooting 特征
TS_ERROR_WORDS = (
    "error", "failed", "fail", "exception", "traceback", "panic",
    "报错", "异常", "堆栈", "崩溃", "起不来", "启动失败", "挂了",
)
TS_ERROR_CODE = re.compile(r"\b(1[0-9]{4}|[45][0-9]{2}|1[34][0-9]|E[0-9]{4,5})\b")
TS_PORT_HINT = re.compile(r"(端口|port).{0,12}(起不来|不通|占用|被占|没监听|listen)")


def _matches(patterns: tuple[str, ...], text: str) -> bool:
    low = text.strip().lower()
    return any(re.search(p, low, re.IGNORECASE) for p in patterns)


def rule_route(question: str) -> str | None:
    """规则层判定。命中返回 route，不确定返回 None。"""
    q = question.strip()
    if not q:
        return None

    # 2. smalltalk（强命中）
    if _matches(SMALLTALK_PATTERNS, q):
        return "smalltalk"

    # 3. out_of_scope（强命中，优先于 troubleshooting）
    if _matches(OUT_OF_SCOPE_PATTERNS, q):
        return "out_of_scope"

    # 4. troubleshooting 特征
    low = q.lower()
    if any(w in low for w in TS_ERROR_WORDS):
        return "troubleshooting"
    if TS_ERROR_CODE.search(q) and any(w in low for w in ("怎么", "如何", "为什么", "排查", "定位", "处理")):
        return "troubleshooting"
    if TS_PORT_HINT.search(low):
        return "troubleshooting"

    # 5. 其余默认 kb_qa
    return "kb_qa"


def router_node(state: dict, *, budget: Budget, llm_call=None, record_step=None) -> dict:
    """router 节点实现。

    llm_call: 可选，签名 (model: str, prompt: str) -> str。规则不确定时才调用。
    """
    import time

    t0 = time.monotonic()
    question = state.get("question", "")

    route = rule_route(question)
    conf = 1.0
    llm_calls_delta = 0

    if route is None:
        # 规则不确定 → 默认 kb_qa；若提供了 llm_call 则尝试模型兜底
        route = "kb_qa"
        conf = 0.5
        if llm_call is not None and budget.try_acquire("router", PRIORITY_HIGH):
            try:
                raw = llm_call(
                    "router",
                    "判断这句话属于哪类：kb_qa（知识问答）/ troubleshooting（故障排查）/"
                    "smalltalk（寒暄）/ out_of_scope（越界）。只输出一个词。\n"
                    f"输入：{question}",
                )
                parsed = _parse_route(raw)
                if parsed:
                    route, conf = parsed, 0.72
                budget.consume_llm()
                llm_calls_delta = 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("router LLM 兜底失败，回落 kb_qa: %s", exc)

    step = {
        "node": "router",
        "duration_ms": int((time.monotonic() - t0) * 1000),
        "input_digest": _digest(question),
        "output_summary": f"route={route}, conf={conf}, llm_calls={llm_calls_delta}",
        "degraded": False,
    }
    if record_step:
        record_step(step)

    return {"route": route, "route_conf": conf, "_router_llm_calls": llm_calls_delta}


def _parse_route(raw: str) -> str | None:
    """从模型输出里解析 route（容错：允许带引号/标点/解释）。"""
    if not raw:
        return None
    text = raw.strip().lower()
    for candidate in ("out_of_scope", "troubleshooting", "smalltalk", "kb_qa"):
        if candidate in text:
            return candidate
    # 尝试 JSON
    try:
        obj = json.loads(raw)
        r = str(obj.get("route", "")).lower()
        if r in ("kb_qa", "troubleshooting", "smalltalk", "out_of_scope"):
            return r
    except Exception:  # noqa: BLE001
        pass
    return None


def _digest(text: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]