# Agent 工作流设计（LangGraph）

| 项 | 值 |
|---|---|
| 版本 | v1.0 |
| 状态 | 已冻结 |
| 上游 | `docs/prd/PRD.md` v1.5 §6/§7、`docs/architecture/ingest-consistency.md` v1.1、`docs/api/ingest-contract.md` v1.1 |
| 约束 | 只引用冻结文档与 `spike-report.md`，**不新增数值口径** |

> 节点口径：**5 业务节点 + 2 确定性终止节点**（PRD §6.2）。Query Rewrite 是 Retrieval 的内置子步骤，**不是节点**。

---

## 1. 图结构

```
START
  │
  ▼
router ──smalltalk──────► smalltalk ──► END
  │      └─out_of_scope─► out_of_scope ─► END
  │
  ├──kb_qa / troubleshooting
  ▼
retrieval ──(chunks 为空 或 最高分 < min_score)──► refuse ──► END
  │
  ├──kb_qa─────────────► evidence ──► final ──► END
  │
  └──troubleshooting──► troubleshoot ──► evidence ──► final ──► END
```

**边类型**：`router` 后为条件边（`add_conditional_edges`），其余为静态边。`refuse` / `smalltalk` / `out_of_scope` 为终止节点，直接 → END。

---

## 2. State schema（字段级）

`app/graph/state.py` 定义 `TypedDict`（LangGraph 1.x 用 `StateGraph(TypedDict)`）：

| 字段 | 类型 | 由谁写 | 说明 |
|---|---|---|---|
| `request_id` | `str` | 入口 | 全链路追踪 id，来自 Go 的 `X-Request-Id` |
| `session_id` | `str` | 入口 | 会话 id |
| `turn_index` | `int` | 入口 | 当前轮次，从 0 开始（首轮 = 0） |
| `question` | `str` | 入口 | 用户原始问题（≤500 字，超长在 Go 侧已拒绝） |
| `history` | `list[Message]` | 入口 | 最近 **4 轮 / 8 条**（PRD FR-31），按时间升序 |
| `route` | `Literal["kb_qa","troubleshooting","smalltalk","out_of_scope"]` | router | 路由结果 |
| `route_conf` | `float` | router | 0~1；规则命中记 1.0 |
| `rewritten_query` | `str \| None` | retrieval(rewrite) | 改写后的检索语句；未改写为 `None` |
| `chunks` | `list[RetrievedChunk]` | retrieval | 见 §2.1 |
| `tool_calls` | `list[ToolCallRecord]` | troubleshoot | 见 §2.2 |
| `draft_answer` | `str \| None` | troubleshoot / final | 未校验的答案文本 |
| `citations` | `list[Citation]` | final / evidence | 见 §2.3 |
| `check_result` | `CheckResult \| None` | evidence | `{supported, invalid_citations, unsupported_claims, method}` |
| `final_text` | `str \| None` | final / refuse / smalltalk / out_of_scope | 最终回复 |
| `answer_type` | `Literal["answered","refusal","clarify","smalltalk","out_of_scope","degraded"]` | 终止节点 | 供前端与企业判分使用 |
| `degraded` | `bool` | 任意节点 | 任一处降级即置 `true`，**只增不减** |
| `degrade_reasons` | `list[str]` | 任意节点 | 枚举见 §6 |
| `preempted` | `list[str]` | runner | 被预算抢占跳过的节点名 |
| `terminated_reason` | `Literal["completed","budget_exhausted","deadline_exceeded","step_limit"]` | runner | 终态原因 |
| `usage` | `Usage` | runner | `{llm_calls, tool_calls, prompt_tokens, completion_tokens}` |

### 2.1 RetrievedChunk

```json
{"chunk_id":"<uuid>","doc_id":"<uuid>","doc_title":"k8s-pod-troubleshooting.md",
 "score":0.83,"content":"……切片原文……","heading_path":"2. 内存与探针配置 > livenessProbe 与 readinessProbe 的区别"}
```

### 2.2 ToolCallRecord

```json
{"tool_call_id":"tc_1","tool_name":"analyze_error_log","status":"ok",
 "args_digest":"sha256:ab12cd34","duration_ms":37,"output_summary":"error_type=connection_refused, lines=3"}
```

> `args_digest` 只存摘要，不存原始入参（用户日志可能含敏感信息，PRD NFR-S5）。

### 2.3 Citation

```json
{"chunk_id":"<uuid>","doc_id":"<uuid>","doc_title":"…","snippet":"≤200字","score":0.83,
 "source_type":"kb","status":"live"}
```

`source_type ∈ {kb, tool, log}`：知识库结论用 `kb`；日志/工具结论用 `tool`/`log` 并填 `tool_call_id`（PRD FR-18 三类证据）。

---

## 3. 节点 IO 契约

### 3.1 router（规则优先，LLM 兜底）

**输入**：`question`, `history`, `turn_index`
**输出**：`route`, `route_conf`, `usage.llm_calls += 0|1`

```json
// 规则命中（0 次调用）
{"route":"troubleshooting","route_conf":1.0}
// 规则不确定（1 次调用，JSON mode，max_tokens=64，模型 LLM_MODEL_ROUTER）
{"route":"kb_qa","route_conf":0.72}
```

判定顺序（PRD §7.1，命中即短路）：`非法输入`（Go 侧已拦）→ `smalltalk` → `out_of_scope` → `troubleshooting` → `kb_qa`。

### 3.2 retrieval（纯代码 + 可选 rewrite 子步骤）

**输入**：`question`, `history`, `turn_index`
**输出**：`rewritten_query`, `chunks`, 可能 `degraded`

```json
{"rewritten_query":"Kubernetes Pod 的 livenessProbe 和 readinessProbe 应该怎么配置？",
 "chunks":[{"chunk_id":"…","score":0.83,"content":"…"}]}
```

rewrite 触发条件（三重全部满足，否则**不调模型**）：`turn_index ≥ 1` ∧ 命中指代词典（它/那/这个/还有/继续）∧ `history` 中存在 assistant 消息。
失败（超时/空输出）→ `rewritten_query=null` + `degraded=true` + `degrade_reasons+=["rewrite_failed"]`，**不阻断**。

### 3.3 troubleshoot（LLM + 工具）

**输入**：`question`, `chunks`, `history`
**输出**：`draft_answer`, `tool_calls`, `usage.llm_calls += 1|2`, `usage.tool_calls += n`

```json
{"draft_answer":"从日志看是 connection refused ……",
 "tool_calls":[{"tool_name":"analyze_error_log","status":"ok","duration_ms":37}]}
```

前置：`route=troubleshooting` 且输入含日志特征。无日志线索时节点被跳过（静态边直连 evidence）。

### 3.4 evidence（规则优先，LLM 兜底）

**输入**：`draft_answer`, `chunks`, `citations`（若上游已产出）
**输出**：`check_result`, `citations`（可能被裁剪）, `usage.llm_calls += 0|1`

```json
{"check_result":{"supported":true,"method":"rule","invalid_citations":[],"unsupported_claims":[]}}
```

规则层（**必定执行，不调模型**）：每个 `citation.chunk_id` 必须存在且属于本次 `chunks` 集合；不在集合内 → 移入 `invalid_citations` 并删除该引用。
LLM 兜底层（仅当规则判"存疑"且答案含强断言时调用，模型 `LLM_MODEL_CHECKER`，temperature=0，max_tokens=256）。

### 3.5 final（LLM，可直通）

**输入**：`draft_answer | check_result`, `citations`, `degraded`
**输出**：`final_text`, `citations`, `answer_type`

直通条件：`route=kb_qa` ∧ `check_result.supported=true` ∧ `degraded=false` → 直接用 `draft_answer`，**不调模型**。

### 3.6 refuse / smalltalk / out_of_scope（纯代码）

```json
{"final_text":"未在知识库中找到与该问题相关的依据。检索到的最高分片段是……（score=0.21）","answer_type":"refusal"}
```

`refuse` 必须附"最高分片段 + 分数"（PRD §7.4 X6）。

---

## 4. 路由条件边伪码

```python
def route_after_router(state: State) -> str:
    return {"kb_qa": "retrieval",
            "troubleshooting": "retrieval",
            "smalltalk": "smalltalk",
            "out_of_scope": "out_of_scope"}[state["route"]]

def route_after_retrieval(state: State) -> str:
    if not state["chunks"] or state["chunks"][0]["score"] < MIN_SCORE:
        return "refuse"
    if state["route"] == "troubleshooting":
        return "troubleshoot"
    return "evidence"

def route_after_evidence(state: State) -> str:
    return "final"          # 恒进入 final；证据不足只影响 citations 与 answer_type
```

> `refuse` 判定用**最高分**而非平均分，避免"一条高分 + 四条噪声"被平均拉低后误拒。

---

## 5. 预算、抢占与 reserved_for_final 的执行点

### 5.1 常量（全部来自 `.env` / 冻结文档）

| 常量 | 值 | 来源 |
|---|---|---|
| `MAX_STEPS` | 8 | PRD §6.3 |
| `MAX_LLM_CALLS` | 6 | PRD §6.3（D14） |
| `RESERVED_FOR_FINAL` | 1 | PRD §6.3 |
| `MAX_TOOL_CALLS` | 3 | PRD §6.3 |
| `WALL_CLOCK_MS` | 60000 | PRD §6.3 |
| `T_ROUTER` / `T_REWRITE` / `T_RETRIEVAL` / `T_TROUBLESHOOT` / `T_CHECKER` / `T_FINAL` | 8000 / 5000 / 6000 / 15000 / 10000 / 15000 | PRD §6.3（合计 **59s ≤ 60s**） |
| `LLM_MODEL_ROUTER` / `..._QA` / `..._TROUBLESHOOT` / `..._CHECKER` / `..._FINAL` | `qwen-flash` / `qwen-plus` ×4 | `.env` |
| `RETRIEVAL_TOP_K` / `RETRIEVAL_MIN_SCORE` / `HNSW_EF_SEARCH` | 5 / 0.35 / 100 | `.env`（`ef_search` 来自 S1 实测） |

### 5.2 三个检查点

| 检查点 | 位置 | 行为 |
|---|---|---|
| **CP1 入口** | `run_turn()` 开始 | 初始化 `usage`、锁定 `RESERVED_FOR_FINAL`（可用额度 = `MAX_LLM_CALLS - 1 = 5`）、记录 `deadline = now + WALL_CLOCK_MS` |
| **CP2 每次 LLM 调用前** | `llm_client.chat()` 包装内 | ①已用 ≥ `MAX_LLM_CALLS` → 抛 `BudgetExhausted`；②`now > deadline` → 抛 `DeadlineExceeded`；③节点需抢占时按 §5.3 顺序判定 |
| **CP3 每次工具调用前** | `tool_executor()` 内 | `tool_calls` 已达 3 → 跳过并 `degraded=true`（不抛错，工具非必需） |

### 5.3 抢占顺序（额度不足时）

```
final(1, 锁定不可抢) > router > troubleshoot 第1次 > troubleshoot 第2次 > checker LLM > rewrite
```

实现：`Budget.reserve_final()` 在 CP1 预留 1；`Budget.try_acquire(node)` 对 P0 节点（final）恒成功，其余按剩余额度返回 bool；失败时节点执行"无模型分支"并写 `preempted`。

### 5.4 终止

| 触发 | `terminated_reason` | 后续 |
|---|---|---|
| 正常走完 final/refuse 等 | `completed` | — |
| `BudgetExhausted` | `budget_exhausted` | 若 final 尚未执行 → 用 `draft_answer` 模板兜底；否则原样返回 |
| `DeadlineExceeded` | `deadline_exceeded` | 同上，并 `degraded=true` |
| `steps > MAX_STEPS` | `step_limit` | 同上 |

---

## 6. 降级原因枚举

`rewrite_failed` / `router_llm_failed` / `troubleshoot_timeout` / `tool_failed` / `tool_skipped_budget` / `checker_timeout` / `checker_llm_skipped` / `final_timeout` / `embedding_failed` / `budget_exhausted` / `deadline_exceeded`

前端角标映射（PRD §7.5）：`MODEL_TIMEOUT`→"未完成深度校验"；`budget/deadline`→"省略了部分校验"；`tool_*`→"工具调用失败，以下结论仅基于文档"。

---

## 7. trace 落库格式

### 7.1 `agent_runs`（Go 写，一轮一条）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid | run id |
| `session_id` / `message_id` / `owner_id` | uuid/text | 归属 |
| `request_id` | text | 与日志串联 |
| `route` | text | 最终路由 |
| `node_path` | text[] | 实际执行顺序，如 `{router,retrieval,evidence,final}` |
| `llm_calls` / `tool_calls` / `prompt_tokens` / `completion_tokens` | int | 用量 |
| `degraded` | bool | 是否降级 |
| `degrade_reasons` | text[] | §6 枚举 |
| `preempted` | text[] | 被抢占的节点 |
| `terminated_reason` | text | §5.4 |
| `duration_ms` | int | 端到端 |
| `created_at` | timestamptz | — |

### 7.2 `run_steps`（每节点一条，Go 写）

```json
{"run_id":"…","step_index":2,"node":"retrieval","duration_ms":412,
 "input_digest":"sha256:9f2c…","output_summary":"chunks=5, top_score=0.83, rewritten=true",
 "degraded":false}
```

> `input_digest` 是摘要不是原文；原文只在 trace 调试模式（`.env` 开关）下写入，默认关。

### 7.3 `tool_calls`（Go 写）

字段见 PRD §5.5 与 data-model §2.5：`run_id, step_index, tool_name, args_digest, status, duration_ms`。

---

## 8. 编排接口：SSE 与非流式共用

**唯一入口**（Python 内部）：

```python
def run_turn(req: TurnRequest, emit: Callable[[Event], None] | None = None) -> TurnResult:
    """同步执行一轮。emit=None 即非流式；emit 非空则在关键点回调事件。"""
```

| 调用方 | 用法 |
|---|---|
| 非流式接口 `POST /internal/v1/chat` | `run_turn(req, emit=None)`，返回完整 `TurnResult` |
| 流式接口 `POST /internal/v1/chat/stream` | `run_turn(req, emit=sse_writer)`，把 `Event` 逐条写成 SSE |

**Event 类型**（与 PRD §8.5 的 S1–S6 对齐）：`meta` / `delta` / `citation` / `done` / `error`。
`citation` **只在 final 之后一次性发出**，不在流中插入半截引用。

**共用保证**：
1. 两条路径走同一个 `run_turn`，**不允许**存在第二套编排代码。
2. 流式断连时 `emit` 抛 `ClientDisconnected`，`run_turn` 捕获后停止后续节点、落 `terminated_reason=client_disconnected`、不再发起新的 LLM 调用（PRD §8.5 S4）。
3. 落库逻辑在 `run_turn` 返回后统一执行一次，不在流中增量写库。

---

## 9. 未在本文件解决的问题

| # | 问题 | 归属 |
|---|---|---|
| 1 | LangGraph 1.2.16 的 `StateGraph` 与 `add_conditional_edges` 具体签名 | S6 开工前用最小示例验证（R5） |
| 2 | Router LLM 的 prompt 与 few-shot | S6 实现时定，评测集 A5 验收 |
| 3 | `citation` 事件是否需要分页 | 取决于 PRD §11.5 U4 实测体验 |
