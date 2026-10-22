# RAG 评测规范（V1）

| 项 | 值 |
|---|---|
| 版本 | v1.0 |
| 状态 | 已冻结 |
| 上游 | `docs/prd/PRD.md` v1.5 §9、`docs/testing/spike-report.md` |
| 数据 | `evals/datasets/qa_golden.jsonl`（35 条）、`scripts/seed_docs/`（5 篇） |
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

### 1.4 Gold 复核（X5，待你执行）

35 条全部由我起草，**尚未经人工复核**。复核要求：

- 逐条确认 `gold_chunk_texts` 是否真的是"回答该问题所需的最小证据集"；
- 确认 `no_doc` 6 条确实无法从 5 篇语料推出答案；
- 确认 `citation_risk` 3 条的"正确答案"与语料表述一致（这 3 条直接决定"内容支持率"能否被诚实测量）。

复核完成前，报告中的指标只能标注 **`标注未复核`**，不得写进 README 卖点。

---

## 2. 指标

| 指标 | 公式 | 计算方式 | 阈值 |
|---|---|---|---|
| **Recall@5** | `Σ\|Top5 ∩ Gold\| / Σ\|Gold\|`（micro） | 自动：按 `gold_chunk_texts` 在返回切片中做子串匹配 | ≥ 0.75 |
| **MRR@5** | `(1/N)·Σ(1/rank_i)`，Top5 无相关计 0 | 自动 | ≥ 0.60 |
| **引用 ID 有效率** | `有效 ID 引用数 / 总引用数` | 自动：`chunk_id` 存在且属于本次检索集 | **100%**（硬性） |
| **引用内容支持率** | `被支撑引用数 / 抽检引用数` | 人工抽检 20 条 | ≥ 0.90 |
| **拒答正确率** | `正确拒答数 / 应拒答样本数` | 自动判 `answer_type=refusal`；人工抽检措辞 | ≥ 0.85 |
| **过度拒答率** | `应作答却被拒数 / 应作答样本数` | 自动 | ≤ 0.15（观察指标） |
| **任务成功率** | `成功样本数 / 总样本数` | 人工二值判定：答案正确 + 引用有效 + 无幻觉 | ≥ 0.70 |
| **多轮保持率** | `ΣRecall_A / ΣRecall_B` | 自动（见 §3） | ≥ 0.90 |
| **延迟** | P50 / P95，分检索段与端到端 | 自动打点 | kb_qa P95 ≤ 8s；log_diag P95 ≤ 25s |

> 为什么把"引用 ID 有效率"和"内容支持率"分开：前者是**引用是否存在**（代码可断言），后者是**引用是否支撑结论**（必须人看）。合并成一个数会掩盖"引用都存在但都不支持"这种最危险的情况。

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

---

## 4. 执行

```bash
cd ai-service
.venv/Scripts/python.exe -m evals.run_eval \
  --dataset ../evals/datasets/qa_golden.jsonl \
  --corpus ../scripts/seed_docs \
  --out ../evals/reports/$(date +%Y%m%dT%H%M).json
```

执行前置：语料已摄取且全部 `completed`；`HNSW_EF_SEARCH` 按 `.env`；同一进程内串行执行（避免 429，实测并发上限≈30）。

### 4.1 报告结构（JSON）

```json
{
  "meta": {"dataset_version":"v1.0","git_commit":"<sha>","run_at":"...","models":{"llm":"qwen-plus","embedding":"text-embedding-v4"},
           "params":{"top_k":5,"min_score":0.35,"ef_search":100},"gold_reviewed":false},
  "per_question":[{"id":"q001","recall5":1.0,"mrr":1.0,"citations":[{"chunk_id":"...","id_valid":true,"supports":null}],"latency_ms":{"retrieval":120,"total":3200},"answer_type":"answered"}],
  "summary":{"recall5":null,"mrr5":null,"citation_id_valid_rate":null,"citation_support_rate":null,
             "refusal_accuracy":null,"over_refusal_rate":null,"task_success":null,"multiturn_retention":null,
             "not_comparable":[],"latency_p50_ms":null,"latency_p95_ms":null}
}
```

### 4.2 真实性规则

1. 未跑的指标值一律 `null` 并在 Markdown 摘要写 **`NOT RUN`**，禁止填 0、禁止填目标值。
2. `meta.gold_reviewed=false` 时，摘要顶部必须标注 **`标注未复核`**。
3. 报告必须含 `git_commit`（A12 要求）。
4. 人工判定项（内容支持率、任务成功率）必须记录判定人与时间，不接受"看起来对"。

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
