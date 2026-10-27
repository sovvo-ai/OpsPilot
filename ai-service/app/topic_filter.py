"""V1.1 s5：检索主题一致性过滤器（确定性规则，零 LLM 调用）。

**为什么需要**（2026-10-27 外部审查指出的致命缺陷）：
critic 的启用范围是 `route=troubleshooting`，但越界缺陷的原型 m001（探针配置）
实测走 `route=kb_qa`（无错误特征）。**治病的路和发病的路错开了。**
根因在检索层：无关文档混入引用池 → 模型挑错编号。

**规则**（确定性、零成本、全路径生效）：
1. 从 query / rewritten_query 提取**主题锚点**（技术标识符 + 中文名词）
2. 对每个 chunk，检查 `heading_path` 与正文是否含锚点（或其同义项）
3. 无锚点 → 剔除；锚点只出现在正文深处（非主题相关）→ 降权

**与 critic 的分工**：filter 治**主因**（污染进入引用池），critic 治**次因**
（模型挑错编号）。二者可叠加，也可单用 —— 由三臂对照数据裁定。
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# 锚点提取
# ---------------------------------------------------------------------------

# 技术标识符（camelCase / snake_case / Probe 后缀 / 错误码短语）
_IDENT_RE = re.compile(
    r"\b("
    r"[a-z][a-z0-9]*[A-Z][A-Za-z0-9]*"          # camelCase: initialDelaySeconds
    r"|[a-z]+_[a-z0-9_]+"                        # snake_case: proxy_read_timeout
    r"|[A-Za-z]+(?:Probe|Timeout|Seconds|Limit)"  # livenessProbe
    r")\b"
)

# 错误码 / 错误短语（**最强主题信号**：出现即锁定主题）
# 实测教训（2026-10-27 s5 首轮）：只切中文词会漏掉 `Exit Code 137`、
# `Connection refused`、`Too many connections`、`Lock wait timeout` 这些
# **真正的主题标识**，反而把"一般代表""有什么区"这类疑问短语当锚点 →
# 误杀 5 题（Recall 0.9259→0.7407）。
_ERR_CODE_RE = re.compile(
    r"\b("
    r"[A-Z][a-z]+\s+Code\s+\d{2,3}"              # Exit Code 137
    r"|[A-Z][a-z]+#\d{3,}"                        # Error#1040
    r"|Error\s+\d{3,}"                            # Error 1040
    r"|\b[1-5]\d{2}\b"                            # HTTP 状态码 502 / 404
    r")\b"
)

# 多词错误短语（英文，区分度高）
_ERR_PHRASE_RE = re.compile(
    r"("
    r"connection\s+refused|connection\s+timed\s+out"
    r"|too\s+many\s+connections"
    r"|lock\s+wait\s+timeout"
    r"|crashloopbackoff|imagepullbackoff|oomkilled|evicted|pending"
    r"|bad\s+gateway|gateway\s+timeout"
    r"|no\s+space\s+left|permission\s+denied"
    r")",
    re.IGNORECASE,
)

# 疑问/泛化短语：出现在 query 里**绝不能**当锚点
# （实测误杀来源：这些词被当锚点 → 只匹配到同样含这些词的无关文档）
INTERROGATIVE = re.compile(
    r"(一般代表|有什么区|是什么|怎么定位|原因是什么|这是什么|如何配置|怎么排查|"
    r"应该怎么|怎么办|有哪些|能不能|可不可以|是什么意思)"
)

# 中文主题词：从 query 中提取的实义名词（简化实现：领域词表 + 后缀匹配）
# 不做通用分词 —— 那是引入新依赖，且会带来不确定性。用**领域词表**更可控。
DOMAIN_TERMS = {
    # K8s
    "探针": ["探针", "probe", "liveness", "readiness", "startup", "健康检查"],
    "pod": ["pod", "容器", "kubelet"],
    "crashloopbackoff": ["crashloopbackoff", "重启", "退避"],
    "oom": ["oom", "内存", "内存不足", "137"],
    "镜像": ["镜像", "image", "pull"],
    # Nginx
    "nginx": ["nginx"],
    "502": ["502", "bad gateway"],
    "504": ["504", "gateway timeout"],
    "upstream": ["upstream", "上游"],
    "超时": ["超时", "timeout", "proxy_read_timeout", "proxy_connect_timeout"],
    # MySQL
    "mysql": ["mysql"],
    "索引": ["索引", "index", "explain", "全表扫描"],
    "慢查询": ["慢查询", "slow_query", "long_query_time"],
    # 网络
    "网络": ["网络", "network", "ip ", "路由", "网关", "dns"],
    "端口": ["端口", "port", "listen", "监听"],
    "连接": ["连接", "connection", "refused", "timed out"],
    # 日志
    "日志": ["日志", "log", "logging"],
}

# 泛化词：不能单独作为锚点（否则"配置""怎么""问题"会把所有文档都放进来）
STOPWORDS = {
    "配置", "怎么", "如何", "什么", "为什么", "区别", "方法", "步骤", "问题",
    "应该", "需要", "可以", "一下", "这个", "那个", "它", "那",
    "如何配置", "怎么配", "是什么", "有哪些", "怎么办",
}

# ⚠️ "pod" / "容器" 单独不能作为锚点 —— 它们在整个 k8s 文档集里几乎处处出现，
# 用作锚点等于不过滤（实测：linux-network chunk 因正文含"容器"二字拿到 0.3 分）。
# 这类"全集分布词"必须靠**共现**才有意义：见 `_has_generic_only` 的处理。
GENERIC_TERMS = {"pod", "容器", "kubelet", "服务", "系统", "数据"}

MIN_ANCHOR_LEN = 3

# 强锚点：含这些词的锚点命中即视为"主题强相关"，不受正文位置权重压制。
# 依据：错误码/错误短语在运维文档里是**唯一标识**，不会作为无关内容出现。
_STRONG_ANCHOR_MARKERS = (
    "probe", "refused", "timed out", "timeout", "crashloop", "oomkill",
    "too many connections", "lock wait", "exit code", "bad gateway",
    "gateway timeout", "no space", "permission denied", "1040", "137",
)


def extract_anchors(query: str) -> list[str]:
    """从 query 提取主题锚点。返回去重后的关键词列表（含同义项）。

    **优先级（实测教训：顺序错了会误杀）**：
      ① 错误码 / 错误短语 —— 最强的主题信号（`Exit Code 137`、`Connection refused`）
      ② 技术标识符 —— `livenessProbe` / `proxy_read_timeout`
      ③ 领域词表 —— "探针" → 展开同义项
      ④ 中文实义词 —— 兜底，且**必须过滤疑问短语**

    若 ①②③ 都提取不到任何锚点，说明这条 query 缺主题信号 ——
    此时返回空列表，**过滤器自动跳过**（不过滤比误杀安全）。
    """
    if not query:
        return []

    anchors: set[str] = set()
    q_lower = query.lower()

    # ① 错误码 / 错误短语
    for m in _ERR_CODE_RE.findall(query):
        anchors.add(m.lower().replace("  ", " "))
    for m in _ERR_PHRASE_RE.findall(query):
        anchors.add(re.sub(r"\s+", " ", m.lower()))

    # ② 技术标识符
    for m in _IDENT_RE.findall(query):
        if len(m) >= MIN_ANCHOR_LEN and m.lower() not in STOPWORDS:
            anchors.add(m.lower())

    # ③ 领域词表
    for key, synonyms in DOMAIN_TERMS.items():
        if key in q_lower:
            for syn in synonyms:
                if len(syn) >= 2:
                    anchors.add(syn.lower())

    # ④ 中文实义词兜底（剔除疑问短语与停用词）
    if not anchors:
        for m in re.findall(r"[\u4e00-\u9fa5]{2,5}", query):
            if m in STOPWORDS or m in GENERIC_TERMS:
                continue
            if INTERROGATIVE.search(m):
                continue
            anchors.add(m)

    return sorted(anchors)


def _specific_anchors(anchors: list[str]) -> list[str]:
    """剔除通用词，只留**有区分度**的锚点。

    为什么必须分开：`pod`/`容器` 在整个 k8s 文档集里几乎处处出现，
    拿它们当匹配依据会让"任何 k8s 文档"都过筛 —— 等于不过滤。
    但它们在**判断主题方向**时并非无用，故保留在 `anchors` 里供诊断，
    真正打分只用 `_specific_anchors`。
    """
    specific = [a for a in anchors if a not in GENERIC_TERMS]
    return specific if specific else anchors   # 全是通用词时退回全集（避免空匹配）


# ---------------------------------------------------------------------------
# 过滤器
# ---------------------------------------------------------------------------

def _chunk_text(chunk: dict) -> tuple[str, str]:
    """返回 (heading, body)，均小写。"""
    heading = str(chunk.get("heading_path") or "").lower()
    body = str(chunk.get("content") or "").lower()
    return heading, body


def score_topic_consistency(chunk: dict, anchors: list[str]) -> dict[str, Any]:
    """计算单个 chunk 的主题一致性。返回 {score, heading_hits, body_hits, matched}。

    分级（这是关键 —— 不能只看"正文里有没有出现锚点"）：
      - heading 命中：**强信号**（标题就在讲这个主题）→ 权重 3
      - body 前 1/3 命中：中等信号 → 权重 1
      - body 后 2/3 命中：**弱信号**（可能是顺带提及）→ 权重 0.3

    只看"总量命中"会让"正文里偶然出现探针二字"的 chunk 蒙混过关
    （实测 chunk[1]：标题是 Pending，正文里偶含"探针"，实际讲调度失败）。
    """
    heading, body = _chunk_text(chunk)
    # 只用有区分度的锚点打分（通用词会把整个文档集都放进来，见 _specific_anchors）
    scorers = _specific_anchors(anchors)
    if not anchors or not scorers:
        return {"score": 0.0, "heading_hits": [], "body_hits": [], "matched": []}

    heading_hits = [a for a in scorers if a in heading]
    body_hits = [a for a in scorers if a in body]

    # 正文前 1/3 加权更高
    head_third = body[: max(1, len(body) // 3)]
    early_hits = [a for a in scorers if a in head_third]

    # ⚠️ **强信号豁免**（实测修正，2026-10-27）：
    # 错误码/错误短语与含数字的技术标识（`Exit Code 137` / `Connection refused`）
    # 区分度极高 —— 它们出现在片段的**任何位置**都足以认定主题相关。
    # 早期按位置一律给 0.3 权重 → 被判为"顺带提及"→ 直接误杀（q002 等 5 题）。
    strong_hits = [
        a for a in body_hits
        if any(m in a for m in _STRONG_ANCHOR_MARKERS)
        or _ERR_CODE_RE.search(a)
        or re.search(r"\d", a)
    ]
    if strong_hits:
        # 强信号命中即满分（≥ 默认阈值 1.0）
        return {
            "score": round(3.0 + len(strong_hits), 2),
            "heading_hits": heading_hits,
            "body_hits": body_hits,
            "matched": sorted(set(heading_hits) | set(body_hits)),
            "strong": True,
        }

    s = 3.0 * len(heading_hits) + 1.0 * len(early_hits) + 0.3 * (
        len(body_hits) - len(early_hits)
    )
    return {
        "score": round(s, 2),
        "heading_hits": heading_hits,
        "body_hits": body_hits,
        "matched": sorted(set(heading_hits) | set(body_hits)),
        "strong": False,
    }


def filter_by_topic(
    chunks: list[dict],
    query: str,
    *,
    min_consistency: float = 1.0,
    keep_at_least: int = 1,
) -> tuple[list[dict], dict[str, Any]]:
    """按主题一致性过滤/降权 chunk。

    参数：
      min_consistency: 低于此分数的 chunk 被移除（默认 1.0 = 至少要在正文前段或标题命中）
      keep_at_least:   兜底 —— 过滤后若全空，保留原最高分 chunk（避免把答案饿死）

    返回 (过滤后的 chunks, 诊断信息)。
    """
    anchors = extract_anchors(query)
    if not chunks or not anchors:
        return chunks, {
            "anchors": anchors,
            "removed": [],
            "kept": list(range(len(chunks))),
            "reason": "no_anchors" if not anchors else "empty_chunks",
        }

    scored: list[tuple[dict, dict]] = []
    for c in chunks:
        scored.append((c, score_topic_consistency(c, anchors)))

    kept: list[dict] = []
    removed: list[dict] = []
    for c, sc in scored:
        if sc["score"] >= min_consistency:
            kept.append({**c, "_topic_score": sc["score"]})
        else:
            removed.append({
                "doc_title": c.get("doc_title"),
                "score": c.get("score"),
                "topic_score": sc["score"],
                "matched": sc["matched"],
            })

    # 兜底：全被剔除时保留原最高分（空的检索集会让下游退化成拒答，损失更大）
    if not kept and chunks:
        top = max(chunks, key=lambda x: float(x.get("score") or 0))
        kept = [{**top, "_topic_score": 0.0}]
        reason = "fallback_kept_top"
    else:
        reason = "ok"

    return kept, {
        "anchors": anchors,
        "removed": removed,
        "kept": [c.get("doc_title") for c in kept],
        "reason": reason,
    }


__all__ = ["extract_anchors", "score_topic_consistency", "filter_by_topic"]