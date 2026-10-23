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

# 寒暄：**前缀匹配**（B3 小项修正）。
# 早期用全锚定 `^你好[！!。]*$`，导致"你好，你是谁？"这类复合闲聊漏到 kb_qa 再被拒答，
# 观感差。问候类改为前缀命中即短路（PRD §7.1 第 2 档本就是"强命中"语义）。
SMALLTALK_PREFIXES = (
    "你好", "您好", "hi", "hello", "哈喽", "在吗", "在么",
    "谢谢", "感谢", "多谢", "谢了",
    "你是谁", "你是什么", "你能做什么", "你会什么", "介绍一下你", "你叫什么",
)

# 越界：保持正则（需要匹配"帮我写"这类中间出现的模式）
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
TS_PORT_HINT = re.compile(
    r"((端口|port).{0,16}(起不来|不通|占用|被占|冲突|没监听|listen|already in use))"
    r"|((占用|被占|冲突|already in use).{0,16}(端口|port))"
    r"|(\b\d{2,5}\b.{0,20}(端口冲突|端口被占|被占用))",
    re.IGNORECASE,
)


def _matches(patterns: tuple[str, ...], text: str) -> bool:
    low = text.strip().lower()
    return any(re.search(p, low, re.IGNORECASE) for p in patterns)


def _starts_with_any(text: str) -> bool:
    """前缀匹配寒暄词典（B3 小项：全锚定会漏掉复合闲聊）。"""
    low = text.strip().lower()
    return any(low.startswith(p) for p in SMALLTALK_PREFIXES)


# 日志错误特征（S7-A）：与 troubleshoot 的判据保持一致，避免"router 不认、troubleshoot 认"
# 注意两个易错点（实测踩过）：
#   1. `E[A-Z]{3,}`（errno 名）**必须大小写敏感** —— 否则 `sERVER` 这类普通单词
#      会被 IGNORECASE 匹配，把正常日志judged成错误日志。
#   2. 中文否定语境（"无异常"/"未报错"/"没有失败"）不应算错误特征。
_ERRNO_RE = re.compile(r"\bE[A-Z]{3,}\b")          # 大小写敏感
_LOOSE_ERROR_RE = re.compile(
    r"(traceback|exception|critical|\bfatal\b|panic|"
    r"\berror\b|\bfailed\b|\bfailure\b|"
    r"\b[45]\d{2}\b)",
    re.IGNORECASE,
)
_CN_ERROR_RE = re.compile(r"(?<!无)(?<!未)(?<!没)(?<!没有)(错误|异常|失败|报错|堆栈|崩溃|拒绝)")


def _log_has_error_signal(log_text: str) -> bool:
    """附带的日志文本是否含**错误**特征（S7-A）。

    判据要收窄到"错误"而非"任何日志" —— 一段正常的 INFO 日志不该被路由到
    troubleshooting（否则"帮我看看这段日志"配正常日志会误导入口）。
    """
    if not log_text or not log_text.strip():
        return False
    return bool(
        _LOOSE_ERROR_RE.search(log_text)
        or _ERRNO_RE.search(log_text)
        or _CN_ERROR_RE.search(log_text)
    )


def rule_route(question: str, attach_log: str | None = None) -> str | None:
    """规则层判定。命中返回 route，不确定返回 None。

    **S7-A 修正**：判定必须**先看 `attach_log`** —— PRD §2.2-S2 的招牌场景是
    "粘一段日志问『这是什么问题』"，此时问句本身往往不含任何错误特征
    （"帮我看看这段日志"），早期实现只吃 `question`，导致该场景被判 kb_qa
    最终拒答（真实链路上是坏的）。

    判定顺序（命中即短路，**顺序即优先级**）：
      0. **附了日志且日志含错误特征 → troubleshooting**（最高优先，招牌场景）
      1. 输入非法（Go 侧已拦）
      2. smalltalk 前缀命中
      3. out_of_scope 强命中
      4. troubleshooting 特征（问句含堆栈/error/错误码/端口线索）
      5. 其余 → kb_qa
    """
    q = question.strip()
    if not q and not attach_log:
        return None

    # 0. 附日志 + 日志含错误特征 → 最高优先走 troubleshooting（S7-A）
    if attach_log and _log_has_error_signal(attach_log):
        return "troubleshooting"

    if not q:
        return None

    # 2. smalltalk（强命中；前缀匹配以覆盖"你好，你是谁？"这类复合闲聊）
    if _starts_with_any(q):
        return "smalltalk"

    # 3. out_of_scope（强命中，优先于 troubleshooting）
    if _matches(OUT_OF_SCOPE_PATTERNS, q):
        return "out_of_scope"

    # 4. troubleshooting 特征：问句含堆栈/error/错误码/端口冲突线索
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
    attach_log = state.get("attach_log")

    # S7-A：消费 attach_log（附日志提问是 PRD §2.2-S2 的招牌场景）
    route = rule_route(question, attach_log)
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