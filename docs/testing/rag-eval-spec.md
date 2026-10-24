# RAG 评测规范（V1）

| 项 | 值 |
|---|---|
| 版本 | v1.0 |
| 状态 | 已冻结（2026-10-22 S2 审查通过）。仅允许实测结果触发修改 |
| 上游 | `docs/prd/PRD.md` v1.7 §9、`docs/testing/spike-report.md` |
| 数据 | `ai-service/evals/datasets/qa_golden.jsonl`（35 条）、`scripts/seed_docs/`（5 篇） |
| 执行状态 | **全部 `NOT RUN`** —— 尚无检索实现，本文只定义"怎么算、怎么跑、怎么报" |

---

## 1. 数据集

### 1.1 语料

| 文档 | 主题 | 用途 |
|---|---|---|
| `k8s-pod-troubleshooting.md` | Pod 状态、内存与探针 | 主路径 + 探针易混点 |
| `mysql-slow-query.md` | 慢查询、索引失效、锁等待 | 主路径 + 锁等待日志 |
| `linux-network-troubleshooting.md` | 分层排查、DNS、错误码 | 错误码语义 |
| `nginx-502-upstream.md` | 502/504、upstream 配置 | 超时语义易引错 |
| `observability-logging.md` | 日志级别、字段、request_id | 规范类问答 |

选择原则：**有真实歧义点**（如 `proxy_read_timeout` 不是总耗时、`readinessProbe` 不重启容器），这样"引用正确"与"引用存在"能被区分开。

### 1.2 题目分布（35 条）

| 类别 | 数量 | 目的 |
|---|---|---|
| `kb_qa` | 10 | 主路径召回与作答 |
| `log_diag` | 8 | 故障诊断路径、工具调用 |
| `no_doc` | 6 | 拒答正确率（不得编造） |
| `ambiguous` | 3 | 澄清或保守作答（2 条应拒答、1 条可部分作答） |
| `citation_risk` | 3 | 引用容易"存在但不支持结论"的题 |
| `multiturn` | 5 | 指代改写有效性（各配 1 条同义单轮对照） |

### 1.3 字段定义

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | `q001`–`q030`、`m001`–`m005` |
| `category` | enum | 是 | `kb_qa` / `log_diag` / `no_doc` / `ambiguous` / `citation_risk` / `multiturn` |
| `question` | string | 是 | 用户原始问法 |
| `context_turns` | array | 仅 multiturn | 2~4 条 `{role, content}`，按时间顺序 |
| `control_single_turn_question` | string | 仅 multiturn | 语义等价于"理想改写结果"的单轮问法；**Gold 必须与多轮样本完全一致** |
| `pair_id` | string | 仅 multiturn | 与对照问法配对，用于 A/B 统计 |
| `should_answer` | bool | 是 | `false` 表示应拒答或应澄清 |
| `gold_doc_ids` | array | 应作答时必填 | 种子文档标识（`seed:<文件名>`） |
| `gold_chunk_texts` | array | 应作答时必填 | **原文连续子串**，用于定位 chunk（不用 chunk_id，因为重新摄取后 id 会变） |
| `reference_answer` | string | 是 | 参考答案要点，供人工判分参考，不做字符串比对 |
| `notes` | string | 是 | 该题考察点 |

**硬约束（已在生成时脚本校验）**：
1. 每个 `gold_chunk_texts` 元素必须是对应文档的**连续子串**，否则该题视为数据缺陷。
2. `should_answer=false` 的题不得带 `gold_chunk_texts`。
3. `multiturn` 必须同时具备 `pair_id` / `context_turns` / `control_single_turn_question`。

### 1.4 Gold 复核（X5）

**状态：已复核（用户确认 + 外部审查代验，2026-10-23）。**

复核方式（三段）：

| 段 | 执行者 | 范围与内容 | 状态 |
|---|---|---|---|
| 机械层全验 | 外部审查 | 35 条逐条：① 每元素是否为对应文档的**连续子串**；② 分布合理性；③ 词污染检查 | **已完成** |
| 语义层抽验 | 外部审查 | 4 题裁决被采纳（含措辞与"最小证据集"判定） | **已完成** |
| 用户确认章 | 用户 | 从 A3b 抽检的 20 条中亲眼核验 5 条"引用是否真支撑结论" | **已确认（"X5 确认"，2026-10-23）** |

**引用限制解除**：自本确认起，评测数字可写进 README/演示材料——**前提不变：数值必须来自真实运行且有
报告支撑（A12），未跑的仍标 `NOT RUN`**。run_eval 的 `meta.gold_reviewed` 自此可置 `true`。
（备注：确认时用户尚未逐条亲看 A3b 5 条样本；S8 出报告前建议用 `scripts/a3b_score.py` 补看，
若发现标注缺陷按 A12 程序修订数据集并记版本。）

---

## 2. 指标

| 指标 | 公式 | 计算方式 | 阈值 |
|---|---|---|---|
| **Recall@5** | `Σ\|Top5 ∩ Gold\| / Σ\|Gold\|`（micro） | 自动：按 `gold_chunk_texts` 在返回切片中做子串匹配 | ≥ 0.75 |
| **MRR@5** | `(1/N)·Σ(1/rank_i)`，Top5 无相关计 0 | 自动 | ≥ 0.60 |
| **引用 ID 有效率** | `有效 ID 引用数 / 总引用数` | 自动：`chunk_id` 存在且属于本次检索集 | **100%**（硬性） |
| **引用内容支持率** | `被支撑引用数 / 抽检引用数` | 人工抽检 20 条 | ≥ 0.90 |
| **拒答正确率** | `正确拒答数 / 应拒答样本数` | 自动判 `answer_type ∈ {refusal, clarify}`；人工抽检措辞 | ≥ 0.85 |
| **过度拒答率** | `应作答却被拒数 / 应作答样本数` | 自动 | ≤ 0.15（观察指标） |
| **任务成功率** | `成功样本数 / 总样本数` | 人工二值判定：答案正确 + 引用有效 + 无幻觉 | ≥ 0.70 |
| **多轮保持率** | `ΣRecall_A / ΣRecall_B` | 自动（见 §3） | ≥ 0.90 |
| **延迟** | P50 / P95，分检索段与端到端 | 自动打点 | kb_qa P95 ≤ 8s；log_diag P95 ≤ 25s |

> 为什么把"引用 ID 有效率"和"内容支持率"分开：前者是**引用是否存在**（代码可断言），后者是**引用是否支撑结论**（必须人看）。合并成一个数会掩盖"引用都存在但都不支持"这种最危险的情况。
>
> **S2 审查补漏**：拒答正确率原只认 `answer_type=refusal`，但 PRD §7.3 对"追问脱离上下文"的预期输出是 `clarify`（如 q025/q026）。判定条件已扩展为 `answer_type ∈ {refusal, clarify}`，避免 `ambiguous` 样本被系统性误记为"未拒答"。来源 = S2 审查补漏。

---

## 3. 多轮 A/B 方法

对 5 条 `multiturn` 样本，各跑两次：

| 组 | 输入 | 期望 |
|---|---|---|
| A（多轮） | `context_turns` + `question` | 触发 Query Rewrite |
| B（对照） | 仅 `control_single_turn_question` | **不触发** rewrite（首轮 + 无指代词） |

- `多轮保持率 = ΣRecall_A / ΣRecall_B`（分子分母都只统计有效配对，**不是逐对相除再平均**）。
- **`Recall_B = 0` 的配对剔除**，单列 `NOT_COMPARABLE`，不折算、不代 1.0。
- 剔除超过 2 条（>40%）→ 多轮保持率输出 **`NOT RUN`**，同时把该情况记为数据集缺陷。
- `ΣRecall_B = 0` → 输出 `NOT RUN`，不输出 `Infinity` 或 0。
- B 组若也触发 rewrite → 该样本直接判失败（顺带测到"首轮不复写"）。

### 3.1 阶段约束（硬性，不得顺延）

> **来源：外部审查裁定（2026-10-23）。** S5 阶段 FR-17 指代改写尚未实现，多轮验收用
> `control_single_turn_question` 作**等价单轮**（spec 设计即为该对照关系）。这是**一次性**
> 的阶段适配，不得成为长期欠账。

| 阶段 | 多轮验收要求 |
|---|---|
| S5 | 允许用 `control_single_turn_question` 等价单轮；**真实多轮不纳入 S5 记分** |
| **S6（必须）** | ① 跑 **5 条 multiturn 真实链路**（`context_turns` + `question`）；② 断言 **首轮不复写**（`turn_index=0` 时 `rewritten_query` 必须为 `None`）；③ A/B 配对计算多轮保持率（按 §3 口径） |

**S6 若未完成上述三项，不得宣称"S6 完成"，也不得把多轮保持率写进任何卖点。**
再次顺延（推给 S7+）需外部审查重新裁定，实现方无权自行延期。

---

## 4. 执行

```bash
cd ai-service
.venv/Scripts/python.exe -m evals.run_eval \
  --dataset evals/datasets/qa_golden.jsonl \
  --corpus ../scripts/seed_docs \
  --out evals/reports/$(date +%Y%m%dT%H%M).json
```

执行前置：语料已摄取且全部 `completed`；`HNSW_EF_SEARCH` 按 `.env`；同一进程内串行执行（避免 429，实测并发上限≈30）。

### 4.1 报告结构（JSON）

> **来源 = 外部审查裁定（2026-10-23，S8）**：本节 schema 以 `evals/run_eval.py` 的
> **实际产出为准**回改。原稿用的是 `per_question` / `summary`，实跑后改为下述结构 ——
> 新结构把 **judge 分列**（用户口径 / AI 口径 / 混合口径）落成一级字段，
> 这正是本 spec 自己要求的纪律（§4.2 第 4 条"必须记录判定人与时间"）的落地。
> **一次改对，两边不再漂**：后续如有变更，须同步改 `run_eval.py` 与本文件并记来源。

```json
{
  "meta": {
    "git_commit": "<sha>",
    "started_at": "2026-10-23T13:04:24+00:00",
    "duration_s": 275.7,
    "models": {"router":"qwen-flash","qa":"qwen-plus","troubleshoot":"qwen-plus",
               "checker":"qwen-plus","final":"qwen-plus","embedding":"text-embedding-v4"},
    "params": {"retrieval_top_k":5,"retrieval_min_score":0.35,"hnsw_ef_search":100},
    "gold_reviewed": true,
    "dataset": "evals/datasets/qa_golden.jsonl",
    "total_cases": 35,
    "seeds": {"all_completed": true, "docs": [{"name":"...","doc_id":"...","status":"completed","chunks":2}]},
    "smoke": {"qid":"q001","got_answer_type":"answered","citations":2,"recall_at_5":1.0,"passed":true}
  },

  "metrics": {
    "recall_at_5": 1.0, "mrr_at_5": 0.812,
    "citation_id_validity": 1.0, "citation_id_validity_all_pass": true,
    "refusal_accuracy": 0.875, "over_refusal_rate": 0.0,
    "citation_support_rate": "NOT RUN", "task_success_rate": "NOT RUN",
    "multiturn_retention": 1.0,
    "latency": {"kb_qa_p50_ms":3163,"kb_qa_p95_ms":7360,
                "log_diag_p50_ms":8046,"log_diag_p95_ms":14671,
                "overall_p50_ms":3500,"overall_p95_ms":14000},
    "thresholds": {"recall_at_5":0.75,"mrr_at_5":0.60,"citation_support_rate":0.90,
                   "refusal_accuracy":0.85,"over_refusal_rate":0.15,"task_success_rate":0.70,
                   "multiturn_retention":0.90,"citation_id_validity":1.00,
                   "latency_kb_qa_p95_ms":8000,"latency_log_diag_p95_ms":25000}
  },

  "metrics_by_judge": {
    "用户口径（user_recheck=true，判定人留空）": {"count":5,"citation_support_rate":"NOT RUN",
                                                 "pending_qids":["q028","q029","m001","q001","q008"]},
    "AI 口径（judge=AI）":                    {"count":15,"citation_support_rate":1.0},
    "混合口径（AI 已判 + 用户已填）":           {"count":15,"citation_support_rate":1.0}
  },

  "a3b_reviews": [
    {"qid":"q028","category":"citation_risk","reason":"引用风险题：最易出现'引用存在但不支持结论'",
     "user_recheck":true,"judge":null,"judged_at":null,"verdict":null},
    {"qid":"q002","category":"kb_qa","reason":"常规抽检",
     "user_recheck":false,"judge":"AI","judged_at":"2026-10-23T13:09:00+00:00","verdict":"supported"}
  ],

  "cases": [
    {"qid":"q001","category":"kb_qa","question":"...","should_answer":true,
     "expected_answer_type":"answered","got_answer_type":"answered",
     "route":"kb_qa","node_path":["router","retrieval","evidence","final"],
     "duration_ms":5000,"latency_ms":7360,
     "retrieval_top_score":0.779,"retrieved_chunk_ids":["<uuid>","..."],
     "recall_at_5":1.0,"mrr_at_5":1.0,"citation_id_valid":true,"citation_count":2,
     "tool_calls":[],"final_text_head":"...","error":null,
     "multiturn_ab":{"recall_a":1.0,"recall_b":1.0,"b_triggered_rewrite":false,
                     "retr_a":["..."],"retr_b":["..."]}}
  ]
}
```

**字段口径说明**（防未来误读）：

| 字段 | 说明 |
|---|---|
| `metrics` | 7 项指标 + 延迟 + 阈值。**引用 ID 有效率独立于 7 项**（硬线，`citation_id_validity`） |
| `metrics_by_judge` | **禁止只给合并值**。三口径独立输出；用户未填时其值为 `NOT RUN` |
| `a3b_reviews` | 20 条抽检。`user_recheck=true` 的 5 条 `judge`/`judged_at`/`verdict` 留空等用户填 |
| `cases[].multiturn_ab` | 仅多轮题有值，供 §3 保持率复算（`Σrecall_a / Σrecall_b`） |
| 未跑指标 | 一律字符串 `"NOT RUN"`，**不用 null**（`null` 易被误读为"算出来是空"） |

### 4.2 真实性规则

1. 未跑的指标一律输出 **`NOT RUN`**（字符串）并在 Markdown 摘要同样标注，禁止填 0、禁止填目标值、禁止推算。
2. `meta.gold_reviewed=false` 时，摘要顶部必须标注 **`标注未复核`**。
3. 报告必须含 `git_commit`（A12 要求）。
4. 人工判定项（内容支持率、任务成功率）必须记录判定人与时间，不接受"看起来对"。
5. **阈值未达标不许调数据集凑数**；调参记录必须写进报告"校准日志"。

---

## 5. 失败判定（什么时候不能宣称达标）

| 情况 | 处置 |
|---|---|
| 任一自动指标为 `NOT RUN` | 不得宣称"评测通过" |
| 引用 ID 有效率 < 100% | 视为缺陷，阻断 S8 |
| `no_doc` 有任一条编造了排障步骤 | 视为 P0 缺陷（违反 PRD §7 硬规则） |
| 多轮保持率 < 0.90 | 先调 rewrite，仍不达标则关闭 rewrite 并在 PRD 标注已知限制 |
| 延迟只测了均值没有 P95 | 视为未完成 |

---

## 6. 与冻结文档的关系

本文只把 PRD §9 的指标**落成可执行细节**，不新增口径。任何数值调整（阈值、top_k、min_score）必须在 PRD §14 记变更并说明来源；`ef_search` 的取值来自 S1 实测（`spike-report.md` §1.5、`data-model.md` §2.4）。
