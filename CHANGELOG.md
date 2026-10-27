# CHANGELOG

本项目遵循「如实记录」原则：**达标的写达标，未达标的写未达标，被证伪的方案也留档**。

---

## [v1.1] · 2026-10-27 · 检索主题一致性过滤器

> **本迭代原计划做「Critic Agent（生成后引用审查者）」，经三臂对照实测后改为「检索主题过滤器」。**
> critic 不是失败，是被数据证明**不该现在当主角** —— 相关结论与代码全部存档，作为 V1.2 的立项依据。

### 新增

- **检索主题一致性过滤器**（`ai-service/app/topic_filter.py`）
  - 从 query / rewritten_query 提取主题锚点，剔除与主题无关的检索片段
  - **确定性规则，零 LLM 调用、零超时增量、全路径生效**（`kb_qa` + `troubleshooting`）
  - 锚点提取优先级：错误码/错误短语 → 技术标识符 → 领域词表 → 中文兜底（过滤疑问词）
  - 强信号豁免：错误码与含数字标识（如 `Exit Code 137`）命中**任意位置**均视为强相关
  - 兜底：全部被剔除时保留最高分 chunk（避免把答案饿死）
- 配置项 `TOPIC_FILTER_ENABLED`（**默认 on**）与 `TOPIC_FILTER_MIN_CONSISTENCY`（默认 1.0）
- 剔除明细进 trace：`state.topic_filter` / `result.topic_filter` / step 摘要（`filtered_out` / `anchors`）
- 越界诱饵集 `ai-service/evals/datasets/critic_golden.jsonl`（8 题，含 `bait_points` /
  `expected_verdicts` / `must_not_strip`，含**反向诱饵**（同义注记不许剥离））
- ADR-0002《主药选择：主题过滤优于生成后审查》（Accepted）
- 全局测试约定 **G8**：对照评测必须断言被测机制在生产路径生效（审计字段非空）

### 评测数据（`docs/testing/v1.1-eval-report.md`）

| 赌注 | 达标线 | 实测 | 判定 |
|---|---|---|---|
| FB1 诱饵集越界 ≤ 基线 10% | ≤0.8（3 轮均值） | 基线 **8.0** → **0.67**/轮 | ✅ PASS |
| FB2 主集 Recall 差=0 且 MRR ≥ 基线 | Δ0 / ≥0.7728 | **Δ0.0000** / **0.8704（+0.0975）** | ✅ PASS |
| FB3 延迟配对均值 ≤ +500ms 且同向 | 90 对配对 | **−219.1ms**，95% CI [−746.8, +244.6] | ✅ PASS |
| FB4 误杀回归哨兵 | 存在且绿 | 2 文件 + 60 单测 | ✅ PASS |

**越界流出降 91.6%（8.0 → 0.67/轮，3 轮均值，同次对照）。**

### 变更

- ADR-0001 状态改为 **Superseded（部分）**：其"本迭代主药"结论被 ADR-0002 取代，
  §3–§7（输入契约 / 三态 schema / 处置策略 / 与 checker 边界）**保留供 V1.2 复用**
- PRD delta 升 v1.1-delta-3：赌注重写为 FB1–FB4（预注册，先写数字再动代码）
- `a3b_user_review.py` 修复 q029 瑕疵：清单按 `citations[].index` 展示（不再用数组位置）
- 并发上限复测（新账号）：40 并发全过、50 首现 429、**`Retry-After` 仍为 `None`**
  （旧"S1 实测≈30"已不适用；工程仍按 ≤30 设计）

### 推迟至 V1.2

- **Critic Agent（生成后逐句审查）** —— 立项证据：filter 治不了"正确片段内部不支撑结论"
  的残留（0.67/轮）。critic 范围缩小为「草稿/正文与片段逐句配对审查（`troubleshooting` 路径先行）」
- `run_steps` 新增 `agent_id`（迁移 0004）、usage 按 agent 分账、前端轨迹泳道
  （都属于 critic 的配套，filter 不进图，无 agent 概念）

### 已知限制（必读）

> **过滤参数以 5 篇语料 / 35 题标定，新语料泛化未验证，上线新文档集前须重扫阈值。**

- `min_consistency = 1.0` 在本语料上是最优值，但 `q029` 的 gold chunk 分数**恰好 1.00** ——
  阈值只要抬到 1.01 就误杀。这个边界的宽窄**完全取决于语料**
- 重扫脚本：`scripts/spike_v11/s5b_threshold_scan.py`
- **未根治**：残留 0.67/轮，filter 只判"chunk 属不属于本主题"，不判"这句话是否被该 chunk 支撑"

---

## [v1.0] · 2026-10-24 · 首个完整版本

- Go backend(:8081) + Python ai-service(:8000) + PostgreSQL(pgvector)
- 覆盖 RAG 问答、Agent 故障排查、SSE 流式、原生 JS 前端
- S0–S9 十个阶段全部交付；评测 7 项指标达标（Precision@4 / Hit@4 / 引用支持率等）
- **已知残留**：引用越界未根治（`citation_overreach` 5/5 采样均有命中）；
  kb_qa 延迟余量仅约 200ms