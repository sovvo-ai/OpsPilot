"""非流式问答编排（S5 单节点直链）。

S5 范围（用户指定）：retrieval → prompt → 生成 → 引用提取 → 拒答分支。
S6 才接入 LangGraph 全图（router/evidence/final 等节点）。因此本模块**不引入
LangGraph**，用一个线性函数实现同样的外部契约（TurnRequest → TurnResult）。

契约保持与 openapi-ai.yaml 的 TurnRequest / TurnResult 一致，S6 换全图时
Go 侧与对外接口无需改动。

拒答分支（FR-20 + PRD §7.4）：
  检索为空 或 最高分 < min_score → answer_type=refusal，
  且**附上检索到的最高分片段与分数**（让用户判断是不是文档缺失），
  绝不输出具体排障步骤。

引用规则（FR-18/19/22）：
  只把**本次检索集内**的 chunk 作为引用；生成文本里出现的 chunk 序号若不在
  检索集，一律剔除（代码层强制，不依赖模型自觉）。
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

from app.config import get_config
from app.ingest import _build_client
from app.retrieval import RetrievalError, retrieve, top_score

logger = logging.getLogger("app.chat")

# 引用片段上限（PRD FR-19：snippet ≤ 200 字）
SNIPPET_MAX = 200

# 拒答文案（PRD §7.4：说明"未在知识库找到依据"，并附最高分片段）
REFUSAL_TEXT_NO_CHUNK = (
    "未在知识库中找到与该问题相关的资料。\n\n"
    "请确认相关文档是否已上传并完成摄取；如需排查，建议补充具体文档后重试。"
)
REFUSAL_TEXT_LOW_SCORE = (
    "未在知识库中找到足够相关的资料来回答该问题。\n\n"
    "以下是我检索到的最高分片段（分数 {score}），你可以判断是否是文档缺失：\n"
    "> 来源：{title}\n> {snippet}"
)


class ChatError(Exception):
    """编排失败，可映射为结构化错误码。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


# ---------------------------------------------------------------------------
# Prompt 组装
# ---------------------------------------------------------------------------


def build_prompt(
    question: str,
    chunks: list[dict[str, Any]],
    history: list[dict[str, str]],
    invalid_citations: list[str] | None = None,
) -> str:
    """把检索片段 + 最近 4 轮历史拼成生成用 prompt。

    片段按 `[1] [2] …` 编号，要求模型在结论后标注来源编号，便于提取引用。

    **B2 修正（关键）**：早期 prompt 的第 2 条规则是"若片段不足以回答，请直接说明
    依据不足"，措辞过宽 —— 模型面对"上下文指代 + 片段是具体资料"的组合时会过度
    触发拒答，造成"检索到好证据却拒答"。现改为**正向默认 + 收窄的拒答条件**：
    只要片段与问题主题相关，就应基于片段作答；仅在片段**完全无关**时才拒答。

    `invalid_citations`：evidence 层裁掉的无效引用编号，作为硬约束告知模型不得再引。
    """
    ctx_blocks: list[str] = []
    for idx, c in enumerate(chunks, start=1):
        head = f"（{c['heading_path']}）" if c.get("heading_path") else ""
        ctx_blocks.append(f"[{idx}] 来源《{c['doc_title']}》{head}\n{c['content']}")
    context = "\n\n".join(ctx_blocks) if ctx_blocks else "（无检索结果）"

    hist_lines: list[str] = []
    for m in history:
        role = "用户" if m.get("role") == "user" else "助手"
        hist_lines.append(f"{role}：{m.get('content', '')}")
    history_text = "\n".join(hist_lines) if hist_lines else "（无历史）"

    # 上下文指代消解提示（FR-17 的 prompt 侧配合）
    anaphora_note = ""
    if history and any(w in question for w in ("它", "那", "这个", "那个", "该")):
        anaphora_note = (
            "注意：问题中的指代词（它/那/这个…）应结合【最近对话】与【知识库片段】"
            "确定所指对象，然后在片段中查找该对象的资料作答，**不要因为问句本身简短"
            "就判定依据不足**。\n"
        )

    invalid_note = ""
    if invalid_citations:
        invalid_note = (
            f"不得引用以下编号（已被校验剔除）：{', '.join(invalid_citations)}。\n"
        )

    # S9 引用纪律（来源 = S8 A3b 人工复核实证，缺陷 m001）：
    # 实测发现模型会把**片段之外的通用知识**（如"initialDelaySeconds 应大于启动耗时"、
    # "HTTP 探针非 2xx/3xx 会失败"）挂在**真实有效的 KB 引用号**下。引用 ID 校验
    # （只验存在性）与检索指标都抓不到 —— 属"引用越界"。故在 prompt 层显式区分两类内容。
    return (
        "你是运维知识助手。请**优先依据下方【知识库片段】**回答问题。\n"
        "规则：\n"
        "1. **引用纪律（最重要）**：`[n]` 只用于标注**直接取自该编号片段原文**的结论。\n"
        "   - 若某条建议/参数/机制**在片段中找不到原文依据**，就**不要给它标任何编号**；\n"
        "   - 这类内容如需给出，必须**显式标注**为「（模型通用知识，无 KB 出处）」，"
        "并且**不要与带编号的结论混在同一段**；\n"
        "   - **严禁**把片段外的知识挂在片段编号下 —— 那会让用户以为有据可查，"
        "比不给依据更糟。\n"
        "2. **宁少勿假**：只复述片段内明确写出的操作建议；片段的边界就是你的引用边界。\n"
        "3. 每条**有片段依据**的结论后标注来源编号，格式如 `[1]`；编号只能取用"
        "【知识库片段】中真实存在的编号。\n"
        "4. 只要【知识库片段】中有与问题主题相关的内容，就应基于它作答（即使问句表述简短"
        "或含指代）。**仅当片段与问题主题完全无关时**，才说明「未在知识库找到依据」。\n"
        "5. 不确定的措辞用「可能/建议优先排查」；不要输出与问题无关的内容。\n"
        f"{anaphora_note}"
        f"{invalid_note}\n"
        f"【最近对话】\n{history_text}\n\n"
        f"【知识库片段】\n{context}\n\n"
        f"【问题】{question}\n\n"
        "【回答】"
    )


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def llm_generate(prompt: str, cfg: dict) -> tuple[str, dict[str, int]]:
    """调用 DashScope 兼容接口生成回答，返回 (文本, usage)。"""
    url = cfg["bailian_base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg['dashscope_api_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": cfg["llm_model_qa"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": cfg.get("answer_max_tokens", 1024),
        "temperature": 0.2,
        "stream": False,
    }
    try:
        with _build_client(float(cfg.get("llm_timeout_seconds", 30))) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise ChatError("MODEL_UNAVAILABLE", f"生成调用失败: {exc}") from exc

    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise ChatError("MODEL_BAD_RESPONSE", "生成返回结构异常") from exc

    usage_raw = data.get("usage") or {}
    usage = {
        "prompt_tokens": int(usage_raw.get("prompt_tokens", 0)),
        "completion_tokens": int(usage_raw.get("completion_tokens", 0)),
        "total_tokens": int(usage_raw.get("total_tokens", 0)),
    }
    return text.strip(), usage


# ---------------------------------------------------------------------------
# 引用提取与校验（FR-19 / FR-22）
# ---------------------------------------------------------------------------


def extract_citations(answer: str, chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从回答文本中提取 `[n]` 引用，映射回检索集内的 chunk。

    只保留 `n ∈ [1, len(chunks)]` 的引用；文本里编造的编号一律剔除
    （FR-22：校验在代码层执行，不依赖模型自觉）。

    **S9 修正（角标映射，实测 m001）**：早期实现按"答案中 `[n]` 的**出现顺序**"
    收集，并直接 `citations.append(chunks[n-1])`。于是若答案先出现 `[2]` 再出现 `[1]`，
    则 `citations[0] = chunks[1]`、`citations[1] = chunks[0]` —— 前端按
    "数组下标 i ↔ 正文 `[i+1]`"渲染就会**错位**（正文写 `[1]` 点开却是 2 号片段）。

    现改为：**按 `n` 升序**输出，且每条带显式 `index` 字段。
    前端一律用 `index` 匹配角标，**不得依赖数组下标**。
    """
    used: set[int] = set()
    for m in re.finditer(r"\[(\d{1,2})\]", answer):
        n = int(m.group(1))
        if 1 <= n <= len(chunks):
            used.add(n)

    citations: list[dict[str, Any]] = []
    for n in sorted(used):            # 升序：保证 index 与数组位置一致
        c = chunks[n - 1]
        snippet = c["content"].strip().replace("\n", " ")
        if len(snippet) > SNIPPET_MAX:
            snippet = snippet[:SNIPPET_MAX]
        citations.append(
            {
                # `index` = 正文角标数字（`[n]` 里的 n）。前端按它匹配，不用数组下标。
                "index": n,
                "chunk_id": c["chunk_id"],
                "doc_id": c["doc_id"],
                "doc_title": c["doc_title"],
                "snippet": snippet,
                "score": c["score"],
                "source_type": "kb",
                "status": "live",
            }
        )
    return citations


def validate_citations(
    citations: list[dict[str, Any]], chunks: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """引用存在性校验：每个 chunk_id 必须属于本次检索集。

    返回 (保留的引用, 无效的 chunk_id 列表)。该函数是 A3「引用 ID 有效率 100%」
    的代码层保证。
    """
    valid_ids = {c["chunk_id"] for c in chunks}
    kept: list[dict[str, Any]] = []
    invalid: list[str] = []
    for cit in citations:
        if cit["chunk_id"] in valid_ids:
            kept.append(cit)
        else:
            invalid.append(cit["chunk_id"])
    return kept, invalid


# ---------------------------------------------------------------------------
# 引用越界检测（S9 新增，来源 = S8 A3b 人工复核实证 m001）
# ---------------------------------------------------------------------------

# 待检测的「技术标识符」形态：代码字面量/参数名/探针名等。
# 这类词若出现在**被引片段之外**，几乎必然是从模型自身知识带进来的。
_TECH_TOKEN_RE = re.compile(
    r"\b("
    r"[a-z][a-z0-9]*[A-Z][A-Za-z0-9]*"          # camelCase：initialDelaySeconds
    r"|[a-z]+_[a-z0-9_]+"                        # snake_case：proxy_read_timeout
    r"|[A-Za-z]+(?:Probe|Timeout|Seconds|Limit|Request|Memory)"  # 后缀形态：livenessProbe
    r"|[0-9]{3}\b"                               # HTTP 状态码类：2xx 的 2 已单独处理
    r")"
)
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？\n])")


def detect_citation_overreach(
    answer: str,
    citations: list[dict[str, Any]],
    chunks: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """检测「引用越界」：带 `[n]` 的结论里出现了**该片段没有的技术标识符**。

    **为什么需要这个函数**（S8 m001 实证）：
    `validate_citations` 只校验 `chunk_id` **是否存在**（引用 ID 有效率），
    它无法发现"引用真实存在、但内容不支撑结论"。实测 m001 的答案把片段外的
    `initialDelaySeconds` 建议挂在 `[1]` 下 —— 引用 ID 校验全过，人工语义复核才发现。

    **检测口径（保守，只报高置信越界）**：
    对每个含 `[n]` 的句子，抽出其中的技术标识符 token；若该 token **在编号 n
    对应的片段原文里完全不出现**，记为一次越界。返回明细供校准日志与报告使用。

    **注意**：这是**观测性**检测（写进 trace 便于复核），**不自动删引用** ——
    自动删会掩盖问题（用户看不到"曾经越界"），且可能误删合理表述。
    """
    if not answer or not citations:
        return []

    overreach: list[dict[str, Any]] = []
    by_index = {c.get("index"): c for c in citations if c.get("index")}
    # 编号 → 片段**完整原文**（优先用 chunks，避免 snippet 截断导致误判）
    full_by_chunk = {c["chunk_id"]: c.get("content", "") for c in (chunks or [])}

    for sent in _SENTENCE_SPLIT.split(answer):
        marks = re.findall(r"\[(\d{1,2})\]", sent)
        if not marks:
            continue
        tokens = {
            t for t in _TECH_TOKEN_RE.findall(sent)
            if len(t) >= 6           # 过短的词（如 `Pod`）命中率噪声大，跳过
        }
        if not tokens:
            continue
        for mark in set(marks):
            cit = by_index.get(int(mark))
            if not cit:
                continue
            haystack = full_by_chunk.get(cit.get("chunk_id", "")) or (cit.get("snippet") or "")
            missing = sorted(t for t in tokens if t.lower() not in haystack.lower())
            if missing:
                overreach.append({
                    "mark": int(mark),
                    "chunk_id": cit.get("chunk_id"),
                    "sentence": sent.strip()[:180],
                    "tokens_not_in_source": missing,
                })
    return overreach


# 答案层拒答特征（第二层判定，见 decide_answer_type 文档）
_REFUSAL_MARKERS = (
    "依据不足",
    "未包含",
    "不包含",
    "无法依据知识库",
    "无法回答",
    "未在知识库",
    "没有相关",
    "找不到",
    "无从判断",
    "无法确定",
)


def _looks_like_refusal(answer: str) -> bool:
    """判断生成文本是否**整体**为"依据不足"式拒答。

    **B2 修正（关键）**：早期实现只看"是否含拒答关键词"，误判率高。实测真实案例：
    答案主体正确且有 `[1][2][3]` 引用，仅在**末尾补充**一句"其他超时…未在知识库
    找到依据"，就被整体判为拒答（有证据却拒答，演示事故级且不稳定）。

    判据改为**拒答特征的位置**（比"是否有引用"更本质）：

      - 真正的拒答总在**开门见山**处：特征出现在文本前半段 → refusal
      - "主体作答 + 末尾补充未覆盖部分"是**正确行为**：特征出现在后半段 → answered

    覆盖的三类实况：
      1. 纯拒答（"未在知识库中找到…"）→ 特征在开头 → refusal ✓
      2. 拒答式否定（"依据不足，无法回答该问题[1]"）→ 特征在开头 → refusal ✓
         （FR-18③ 禁止拒答挂引用，此处先判为拒答，再由 final 清空引用）
      3. 主体作答 + 末尾补充（实测 m004）→ 特征在末尾 → answered ✓
    """
    text = (answer or "").strip()
    if not text:
        return True

    first_hit = min(
        (text.find(m) for m in _REFUSAL_MARKERS if m in text),
        default=-1,
    )
    if first_hit < 0:
        return False

    # 以「首个拒答特征之前是否存在**实质内容**」为判据，比纯位置比例更稳：
    #   - 特征之前只有引导语（"知识库未提供该数值[1]。依据不足…" 中的前半句本身
    #     就是拒答语境）→ 仍算整体拒答
    #   - 特征之前已有实质作答（含引用标注 `[n]` 或较长正文）→ 只是末尾补充
    prefix = text[:first_hit].strip()
    prefix_has_citation = bool(re.search(r"\[\d{1,2}\]", prefix))
    # 前缀含引用标注，说明前面已经给出有依据的结论 → 不是整体拒答
    if prefix_has_citation and len(prefix) >= 40:
        return False
    return True


def decide_answer_type(answer: str, citations: list[dict[str, Any]], has_chunks: bool) -> str:
    """判定 answer_type（FR-20 / PRD §7.4）。

    **B2 修正（关键）**：早期把"无引用"也当作拒答条件，导致 answer_type 与
    "模型是否输出了 `[n]` 标注"强耦合。而 final 生成有随机性，模型时而漏标 →
    同一个问题时而 answered、时而 refusal（**演示事故级不稳定**）。

    现改为**只依据语义**判定：
      1. 无检索片段 → refusal（上游已处理）
      2. 答案**整体**呈"依据不足"特征 → refusal（见 `_looks_like_refusal` 的组合判定）
      3. 其余 → answered

    引用是否存在不再决定 answer_type；引用由 `final` 的**兜底归因**保证非空
    （见 `final.py`）。这样"有证据却拒答"被彻底消除，而真正的无依据（refuse 节点）
    与噪声命中（模型整体说依据不足）仍正确拒答。
    """
    if not has_chunks:
        return "refusal"
    if _looks_like_refusal(answer):
        return "refusal"
    return "answered"


# NOTE(S6)：原 S5 线性 run_turn 已删除 —— 编排统一走 app.graph.run_turn（LangGraph 全图），
# 遵守 agent-workflow §8「两条路径共用同一 run_turn，不允许存在第二套编排代码」。
# 本模块保留 prompt / 引用提取 / 拒答判定等被节点复用的工具函数。