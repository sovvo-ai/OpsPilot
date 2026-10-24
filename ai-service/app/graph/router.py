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
# 特殊路由标记：需要澄清（PRD §7.4 第三行）。
# 不是四种正式 route 之一 —— 由 router_node 转成 `_force_answer_type=clarify`
# 并让图走到 smalltalk 终止节点（纯代码输出，零模型调用）。
NEED_CLARIFY = "__need_clarify__"

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
# troubleshooting **强特征**：自身即为日志/错误证据（PRD §7.3 第一条件）
TS_STRONG_WORDS = (
    "error", "failed", "fail", "exception", "traceback", "panic",
    "报错", "异常", "堆栈", "崩溃",
)

# troubleshooting **弱特征**：症状描述词，**必须伴随**其他证据才触发。
#
# **D1 修正（外部审查裁定）**：PRD §7.3 的第一条件是"包含日志/错误特征
# （堆栈、error、failed、错误码、异常类名、**端口号 + "起不来"**等）"——
# 裸短语"起不来"**不在**触发条件内。早期实现把"起不来"放进强命中词，
# 导致 `"它起不来了怎么办？"` 这类**无上下文指代 + 无任何错误证据**的追问
# 被判 troubleshooting → 节点在零证据下自由生成 → 绕过证据闸门、输出
# answered 且 0 引用（实为应走 §7.4 clarify）。这是行为缺陷，非"擦线"。
#
# 现改为：弱特征需与强特征 / 错误码 / 端口线索**同时出现**才算证据。
TS_WEAK_WORDS = (
    "起不来", "起不来了", "启动失败", "挂了", "不通", "没反应", "打不开",
)

# 显式诊断请求（PRD §7.3 第二条件："或用户显式要求诊断"）
#
# **收窄说明（D1 修正）**：早期把"排查/定位/诊断"单独列为触发词，导致
# `"Pod CrashLoopBackOff 怎么排查？"`（纯知识问答）被误判为故障排查。
# §7.3 的原意是"要求对**某段材料**做诊断"（原文举例："帮我看看这段日志"），
# 因此要求出现"日志/报错/这段"等**指代材料**的词，或"帮我看看/分析"这类
# 指向具体对象的请求。泛问"怎么排查"不算。
TS_EXPLICIT_REQUESTS = (
    "帮我看看这段日志", "帮我分析日志", "分析这段日志", "看下这段日志",
    "看看这段日志", "这段日志", "这段报错", "日志如下", "日志见", "日志贴在",
    "帮我看看日志", "帮我分析下日志",
)
TS_ERROR_CODE = re.compile(r"\b(1[0-9]{4}|[45][0-9]{2}|1[34][0-9]|E[0-9]{4,5})\b")
TS_PORT_HINT = re.compile(
    r"((端口|port).{0,16}(起不来|不通|占用|被占|冲突|没监听|listen|already in use))"
    r"|((占用|被占|冲突|already in use).{0,16}(端口|port))"
    r"|(\b\d{2,5}\b.{0,20}(端口冲突|端口被占|被占用))"
    # PRD §7.3 原文明确列举的"**端口号 + 起不来**"（D1 修正补齐）
    r"|(\b\d{2,5}\b.{0,10}(起不来|不通|挂了|没反应|启动失败))"
    r"|((起不来|不通|挂了|没反应|启动失败).{0,10}\b\d{2,5}\b)",
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


def rule_route(question: str, attach_log: str | None = None, history: list | None = None) -> str | None:
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

    # 4. troubleshooting 特征（PRD §7.3：日志/错误特征，或显式要求诊断）
    low = q.lower()
    has_strong = any(w in low for w in TS_STRONG_WORDS)
    has_code = bool(TS_ERROR_CODE.search(q))
    has_port = bool(TS_PORT_HINT.search(low))
    has_weak = any(w in low for w in TS_WEAK_WORDS)
    has_explicit = any(w in q for w in TS_EXPLICIT_REQUESTS)

    # 4a. 显式诊断请求（§7.3 第二条件）。
    #     注意：**若有 attach_log，其内容说了算**（S7-A 语义）——
    #     "帮我看看这段日志" + 正常 INFO 日志 → kb_qa（不该进排障）；
    #     "帮我看看这段日志" + 错误日志 → 已由前面第 0 步（attach_log 错误特征）命中。
    #     仅在**无 attach_log** 时，显式诊断请求本身才作为触发依据。
    if has_explicit and not attach_log:
        return "troubleshooting"

    # 4b. **裸指代硬否决**（D1）。
    #     判据：含指代词 ∧ 无 attach_log ∧ 无错误码 ∧ 无数字端口 ∧ 无具体组件名。
    #     返回特殊标记 `NEED_CLARIFY`，由 router_node 转成 clarify 终止（见下）。
    #
    #     为什么必须硬否决、不能返回 None 交给 LLM 兜底：
    #     实测真实链路 LLM 兜底会判成 troubleshooting（conf=0.72），随后节点
    #     因检索到**噪声片段**（top=0.3645）而"有证据"→ 自由生成 → answered + 引用，
    #     但指代对象是模型**猜**的 —— PRD §7.4 明写"不猜"。
    #
    #     注意：**有上文（history 含 assistant 轮）就不算裸指代** —— 那种情况
    #     指代对象在上下文里，FR-17 会负责改写（`那它怎么配？` + 探针历史 → 正常回答）。
    has_context = bool(history) and any(
        (m or {}).get("role") == "assistant" for m in (history or [])
    )
    if _is_bare_anaphora(q) and not (attach_log or has_code or has_port or has_context):
        return NEED_CLARIFY

    # 4c. 强特征：自身即证据
    if has_strong:
        return "troubleshooting"

    # 4d. 错误码 + 疑问/排查词
    if has_code and any(w in low for w in ("怎么", "如何", "为什么", "排查", "定位", "处理")):
        return "troubleshooting"

    # 4e. 端口线索
    if has_port:
        return "troubleshooting"

    # 4f. 弱特征（"起不来"等）：**必须伴随**强特征/错误码/端口线索
    #     —— 裸症状描述不足以判定为故障排查（D1 修正）
    if has_weak and (has_strong or has_code or has_port):
        return "troubleshooting"

    # 5. 其余默认 kb_qa
    return "kb_qa"


def _is_missing_material(state_or_question) -> bool:
    """用户**要求看材料**（日志/报错片段）但**没有提供**（attach_log 为空）。

    D1：`帮我看看这段日志` 却不附日志 → 属"缺输入"，§7.4 要求澄清而非
    说"未在知识库找到资料"（后者语义是"文档缺失"，两者不同）。

    接受 dict（state）或 str（question）两种入参，便于节点与规则层共用。
    """
    if isinstance(state_or_question, dict):
        q = state_or_question.get("question", "")
        attach = state_or_question.get("attach_log")
    else:
        q = state_or_question or ""
        attach = None
    if attach:
        return False
    return any(w in q for w in TS_EXPLICIT_REQUESTS)


def _is_bare_anaphora(q: str) -> bool:
    """是否为"裸指代"问句（含指代词、但没给出指代对象的证据）。

    D1：`这个报错怎么解决？` / `它起不来了怎么办？` / `那是什么原因` 这类
    追问在上文缺失时**无从判断**，§7.4 要求 clarify + 候选，不应进排障节点。
    此处的保守判据：句首或前半句出现指代词，且**没有任何具体对象**（数字端口、
    组件名、错误码）。
    """
    ANAPHORA = ("这个", "那个", "它", "他", "该", "此", "上述", "刚才", "那个东西")
    if not any(w in q for w in ANAPHORA):
        return False
    # 误判防护（实测）：`该` 在"应**该**怎么用"/"**该**怎么办"里是副词，不是指代词。
    # 仅当它作指示代词（"该服务""该问题"）时才计入 —— 用正则区分。
    if "该" in q and not re.search(r"该(服务|组件|问题|报错|错误|进程|端口|应用|接口)", q):
        # 去掉"该"后若不再含其他指代词，则不算裸指代
        if not any(w in q for w in ("这个", "那个", "它", "他", "此", "上述", "刚才")):
            return False
    # 若句中出现了具体对象（数字/常见组件名），则不算裸指代
    SPECIFIC = re.compile(
        r"\b\d{2,5}\b|"                          # 端口号/错误码数字
        r"(nginx|mysql|redis|kafka|postgres|docker|k8s|kubernetes|pod|java|python|"
        r"tomcat|apache|es|elasticsearch|rabbitmq|zookeeper)",
        re.IGNORECASE,
    )
    return not bool(SPECIFIC.search(q))


def router_node(state: dict, *, budget: Budget, llm_call=None, record_step=None) -> dict:
    """router 节点实现。

    llm_call: 可选，签名 (model: str, prompt: str) -> str。规则不确定时才调用。
    """
    import time

    t0 = time.monotonic()
    question = state.get("question", "")
    attach_log = state.get("attach_log")

    # S7-A：消费 attach_log（附日志提问是 PRD §2.2-S2 的招牌场景）
    # D1：同时传入 history —— 有上文时指代可解析，不算裸指代
    route = rule_route(question, attach_log, state.get("history"))
    conf = 1.0
    llm_calls_delta = 0

    # D1：裸指代 → 硬否决为 clarify（**不调 LLM 兜底**）。
    # 路由落到 smalltalk 终止节点（纯代码输出澄清文案），并标记 answer_type。
    force_answer_type = None
    if route == NEED_CLARIFY:
        route = "smalltalk"
        conf = 1.0
        force_answer_type = "clarify"

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
        "output_summary": (
            f"route={route}, conf={conf}, llm_calls={llm_calls_delta}"
            + (", force=clarify" if force_answer_type else "")
        ),
        "degraded": False,
    }
    if record_step:
        record_step(step)

    out = {"route": route, "route_conf": conf, "_router_llm_calls": llm_calls_delta}
    if force_answer_type:
        out["_force_answer_type"] = force_answer_type
    return out


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