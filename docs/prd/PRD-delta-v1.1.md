# PRD Delta · v1.1（Critic Agent / 引用审查者）

| 项 | 值 |
|---|---|
| 文档版本 | v1.1-delta-1 |
| 状态 | **待外部审查**（实现方不得自行改为"已冻结" —— 计划书附录维护规则 4） |
| 日期 | 2026-10-27 |
| 上游 | `docs/prd/PRD.md` v1.8 §6.9 / §7 / §11 / §12；`docs/testing/v3_residual_notes.md` |
| 变更性质 | **收敛性迭代**：不新增用户可见能力，只收口一个已知缺陷 |
| 范围 | 仅 `troubleshooting` 路径 + 超长答案；`kb_qa` 直通路径**不启用** |

> **本文不是 PRD 的替代品。** 它只记录 v1.1 相对 v1.8 的**增量决策与赌注**，
> 其余一切（错误码、状态机、预算常量、验收项 A1–A15、U1–U10）**继承主文档，不在此复述**。
> 冲突时以主文档为准，并回改本文（维护规则 1/3）。

---

## 1. 存在理由：一个明确未收口的缺陷

### 1.1 缺陷原文（quote，不转述）

`docs/testing/v3_residual_notes.md` 残留 1 的结论段，逐字引用：

> **结论**：检测器把该问题从"机器完全抓不到"变成"可观测、可回归"，
> 但**模型仍会把与问题无关的文档编号挂到结论上**。**禁止在任何对外材料
> （README / pitch / 简历）写「引用越界已根治」。**

同文件第 16–18 行记录的**具体越界实例**：

> - 该检测**不是误报**：逐条核对被引片段的**完整原文**确认，某次答案把 `[2]` 给了
>   《Linux 网络故障排查》片段（该片段**完全不含探针内容**），却用它支撑
>   "livenessProbe 失败会导致容器重启" —— 是真实越界。

### 1.2 复现记录（5 次重跑采样，原文照抄）

`docs/testing/v3_residual_notes.md` 第 7–13 行：

```
#1 overreach=3 tokens=['livenessProbe', 'readinessProbe', 'startupProbe']
#2 overreach=1 tokens=['livenessProbe', 'readinessProbe']
#3 overreach=3 tokens=['livenessProbe', 'readinessProbe', 'startupProbe']
#4 overreach=4 tokens=['livenessProbe', 'readinessProbe', 'startupProbe', 'periodSeconds', 'timeoutSeconds']（示意）
#5 overreach=3 tokens=['livenessProbe', 'periodSeconds', 'startupProbe', 'timeoutSeconds']
→ clean: 0/5
```

**复现条件**（照抄同文件第 33–36 行根因定位）：

> `m001` 的检索集（5 条）稳定混入 `linux-network-troubleshooting`
> 与 `nginx-502-upstream` 等**与探针无关**的文档；rewrite 本身**是生效的**
> （实测 `rewritten_query="如何配置 Pod 的探针？"`、`rewritten=True`），
> 问题是改写后的检索仍带回无关文档，而模型没能稳定守住"编号必须与主题匹配"。

### 1.3 为什么现有机制挡不住

| 现有机制 | 覆盖什么 | 为什么挡不住本缺陷 |
|---|---|---|
| `validate_citations`（FR-22 / A3） | `chunk_id` 是否**存在**于检索集 | 越界的引用 ID **完全有效** —— 它确实存在，只是**内容不支撑该结论** |
| `evidence.checker` LLM（§3.4） | 整份 `draft_answer` 是否被片段支持 | 作用于**整份草稿**、且只在"存疑"时触发；**无逐句配对**、无句级处置 |
| prompt 纪律（`chat.py` 规则 1/3） | 事前提示模型不要越界 | **依赖模型自觉**。实测 5/5 采样仍有命中，说明 prompt 层不足 |
| `detect_citation_overreach` | **事后观测**（写入 trace） | 只观测、**不阻断**——越界句仍原样流出给用户 |

**一句话**：当前所有机制里，**没有一处做"逐句 × 其声明片段"的独立配对审查**。这就是本迭代要补的那一层。

---

## 2. 赌注表（必填，不可事后修改数字）

> **规则**：任一赌注不达标 → 功能开关回滚（`CRITIC_ENABLED=false`）、PRD 记录败因，
> **不改赌注数字**。赌注在开工前锁定，不允许在拿到结果后调阈值。

| # | 赌注 | 判定口径（机器可测） | 基线值 | 达标线 |
|---|---|---|---|---|
| **B1** | critic 后 overreach **流出句数 = 0** | `detect_citation_overreach(final_text, citations, chunks)` 返回的条目数（对 `CRITIC_ENABLED=on` 的全部作答统计） | 5 次采样 `clean: 0/5`（即每次都 ≥1 处命中） | **0 处**，且不出现"删空答案"副作用 |
| **B2** | A3b 用户口径支持率**不降** | 同 20 条抽样口径（`a3b_user_review` 体系）+ 新增 critic 专项集 5–8 题 | v3 = 1.000（5/5，覆盖面有限，见 §5 风险） | **≥ 基线**，且被 critic 剥离的句子经人工复核确认"确实不该保留" |
| **B3** | kb_qa 主链延迟**零劣化** | `scripts/kb_qa_latency.py`，同 10 题 | 方案 A 确认 = 7813ms（P95）；全量 35 题 = 6640ms | **不劣于基线**（kb_qa 路径**不启用 critic**，故理论应为 0 影响；此项验证"真的没被拖慢"） |
| **B4** | troubleshoot P95 **仍 ≤ 25s** | 同 `rag-eval-spec.md:90` 口径（log_diag P95 ≤ 25000ms） | v3 实测值见 `2026-10-24T1435_v3-final.json` | **≤ 25000ms** |

### 2.1 赌注的诚实性说明

- **B2 的基线是弱基线**：1.000 只覆盖"被抽中的 5 条结论句"，不等于"全部句子干净"
  （`v3_residual_notes.md` 第 20 行明写）。所以 B2 的真实含义是"**不退化**"，
  不是"达到完美"。达标**不能**被写成"引用已完全正确"。
- **B1 与 B2 存在张力**：critic 若过度剥离，B1 变好（越界句消失）而 B2 可能下降
  （把本来支撑的句子也删了）。**两者必须同时满足**，这是本迭代真正的难点。
  → 这也是 ADR 里"三态处置"存在的理由（`needs_downgrade_label` 而不是一刀切支持/不支持）。

---

## 3. 范围冻结

### 3.1 启用范围（只这两条）

| 条件 | 说明 |
|---|---|
| `route = troubleshooting` | 故障排查路径。这是 overreach 的高发场景（参数名/探针名密集） |
| **超长答案**（`len(final_text) > 800 字`） | 长答案更可能夹带片段外内容；短答案逐句审查性价比低 |

**判定发生在 `final` 产出之后**（critic 是生成后审查，不是生成前约束 —— 见 ADR §1）。

### 3.2 `kb_qa` 直通路径**明确不启用**，理由必须写清

| 理由 | 依据 |
|---|---|
| **延迟预算已无余量** | kb_qa P95 = 7813ms vs 阈值 8000ms，**仅余 ~200ms**（`v3_residual_notes.md` 第 59–61 行："7.8s 距 8s 仅约 200ms"）。加一次 critic LLM 调用必然破线 |
| **预算已 5/6** | 最坏路径实测 5 次 LLM 调用（见 §4），加 critic = 6 = 上限，**零余量** |
| **收益不对等** | kb_qa 是直通路径（`direct_pass` 条件满足时不调模型），答案短、句数少，overreach 密度低于 troubleshooting |

> **这是"不做"的决定，不是"暂缓"**。若将来要给 kb_qa 也上 critic，必须先在 B3 之外
> 单独立一个延迟赌注 —— 不允许在本迭代偷偷放进来。

### 3.3 明确不做（越界即缺陷）

| 不做 | 归属 |
|---|---|
| subgraph / 独立审查子图 | V1.2 |
| HITL（人工介入审查） | V1.3 |
| 新增评测主集题目（35 题基线**不动**） | 本迭代纪律 |
| 检索层主题过滤 / rewrite 二次收敛 | V2-1 / V2-2（`v3_residual_notes.md` 残留 2，会影响 Recall） |
| 自动改写答案内容（除剥离/改标） | 本迭代只做三态处置，不做"重写" |

### 3.4 critic 专项集（新增，独立于主集）

| 项 | 规定 |
|---|---|
| 规模 | **5~8 题** |
| 标注 | 每题标 `overreach_sensitive=true` + 期望行为 |
| 用途 | **仅本迭代回归** |
| 是否进 README 七指标 | **否**。README 的七指标口径不变，专集结果单列在 `v11-critic-eval-report.md` |

---

## 4. 预算约束（本迭代最大的技术风险，先摆出来）

### 4.1 troubleshoot 最坏路径重算（实测代码位置）

| 节点 | LLM 调用 | 代码位置 |
|---|---|---|
| `router` | 0 或 1 | `router.py:301`（规则不确定时） |
| `retrieval`(rewrite) | 0 或 1 | `retrieval.py:73`（`turn_index≥1` + 指代词 + history） |
| `troubleshoot` | **固定 1** | `troubleshoot.py:306-309` |
| `evidence`(checker) | 0 或 1 | `evidence.py:106-110`（draft 存在且 `not supported` 或强断言） |
| `final` | 0 或 1 | `final.py:204`（troubleshoot draft 存在且 not degraded → 直通，**0**） |
| **小计（最坏）** | **5** | |

`MAX_LLM_CALLS = 6`、`RESERVED_FOR_FINAL = 1`（`budget.py:21-22`）。

### 4.2 ⚠️ 与计划书的一处漂移（实测发现，必须记录）

计划书 §6.3 / §6.4 规划了**"2 次诊断"**（P2 可弃档：「Troubleshooting 第 2 次（工具结果复述）」，
`00-environment-and-plan.md:350`），§6.4 故障排查路径写 **3~6 次**，并在第 368 行写
"即使触发 rewrite + **2 次诊断** + checker 兜底，Final 仍有预留额度可用"。

**实测不符**：`troubleshoot.py:297` 注释写「第 1 次 LLM」，
`:306` 是**唯一**一处 `try_acquire("troubleshoot", ...)` + `llm_call`，
**第 2 次从未实现**（全文 grep `llm_call` 仅 1 处调用点）。

**影响**：

| 口径 | troubleshoot 最坏路径 |
|---|---|
| 计划书（规划 2 次诊断） | router(1)+rewrite(1)+**诊断(2)**+checker(1)+final(1) = **6**（顶满 6，final 靠预留额度） |
| 实测实现（1 次诊断） | = **5**（余 1 次额度） |

**这个差异对本次迭代是好消息**：实现少调 1 次，正好为 critic 腾出 1 次额度
（5 + critic = 6 = 上限，不超）。**但它同时暴露了两件事**：

1. 计划书的"2 次诊断"是**未落地的规划**，`agent-workflow.md` §5.3 抢占顺序表里的
   "troubleshoot 第 2 次（P2 档）"**当前是空档**（无代码）。
2. **不得把"5 次"当成"计划就是 5 次"来引用** —— 它是"实现只做了 1 次诊断"的结果。
   若将来补上第 2 次诊断，critic 的额度和本次赌注 B4 都要重算。

> **处置**：本迭代**不补第 2 次诊断**（越界，属范围外），
> 但在 ADR / 报告里明确标注该漂移，避免后续误读。是否补，留 V2 决策。

### 4.3 结论：**加 critic 后为 6，恰好顶满，零余量**

```
router(1) + rewrite(1) + troubleshoot(1) + checker(1) + final(1) + critic(1) = 6 = MAX_LLM_CALLS
```

**这意味着**：任何一处多调一次（模型重试、末来新增节点），就会触发 `preempted`。
且 `RESERVED_FOR_FINAL` 的语义要求 final 永不被抢 —— critic 若排在 final 之后调用，
它在抢占顺序中的位置必须**低于 final**，被抢占时**静默跳过并标 degraded**（不阻断答案）。

**处置方向（待阶段 2 SPIKE 用实测数据定，本文不预判）**：
- 方案 A：`MAX_LLM_CALLS` 提额到 7（需重算 `T_*` 超时总和 ≤60s）
- 方案 B：critic 不计入 `MAX_LLM_CALLS`（作为"后置质量层"独立配额）—— **需 ADR 论证是否破坏预算语义**
- 方案 C：critic 只在 `checker` 未调用时启用（二者互斥，避免重复审查）

> ⚠️ **本项若在 SPIKE 中证明不可行（无法在预算内完成），必须回到本文件修改赌注可达性，
> 标注"来源=实测"，不得直接进实现。**

---

## 5. 风险与已知不确定

| # | 风险 | 影响 | 处置 |
|---|---|---|---|
| R1 | critic 过度剥离导致 B2 下降 | 答案变空/变残 | 三态处置（`needs_downgrade_label` 而非删除）+ 剥离动作全进 trace 可复核 |
| R2 | B2 基线过弱（1.000 只覆盖 5 句） | "不降"可能是统计假象 | 专项集 5–8 题补充；报告必须同时呈现"抽样口径"与"全量机器口径" |
| R3 | 预算零余量 | critic 经常被抢占，实际上没跑 | SPIKE 定方案；报告必须记录 critic **实际执行率**（不是"配置为 on"就算跑过） |
| R4 | 与 `evidence.checker` 职责重叠 | 重复烧预算 / 结论冲突 | ADR §2 明确分工；实现上二者互斥或明确优先级 |
| R5 | 迁移编号冲突 | 迁移失败 | **用 `0004`**，不用任务书写的 `0003`（已被 `0003_retrieval_stats` 占用） |

---

## 6. 验收判定（本迭代）

| 判定 | 条件 |
|---|---|
| **PASS** | B1–B4 全部达标 + 门禁全绿（`go vet` / `go test` / `pytest`）+ 对照报告含 git commit 与逐题结果 |
| **PARTIAL** | 部分赌注达标 → 按 §2 规则回滚功能开关，报告写败因，**不得打 `v1.1` tag** |
| **FAIL** | B1 未达标（核心目标失败）→ 回滚，PRD 记录，不进 README |

> **「交付完成」≠「验收通过」** —— 与 S0–S9 同一口径。本迭代的最终结论由外部审查出具。

---

## 7. 与主文档的关系（升版时机）

本 delta 通过审查后，阶段 5 才把内容**合并进** `docs/prd/PRD.md`：

| delta 内容 | 合并到主文档位置 |
|---|---|
| critic 功能需求条款（FR 新增） | §6.9 工作流编排 |
| 三态处置的用户可见行为 | §7（新增"引用审查"小节） |
| B1–B4 赌注 → 验收项 | §11.1（A16 `critic 引用审查`） |
| 预算提额的常量变更 | §6.9 FR-36（若 SPIKE 选方案 A） |
| 本迭代决策记录 | §14 修订记录（v1.10） |

**阶段 5 必须做的版本引用回扫**（维护规则 1）：`grep -rn "MAX_LLM_CALLS\|reserved_for_final\|6 次" docs/`，
确认所有引用位置同步 —— 常量为单一权威（`agent-workflow.md` §5.1），其余文档只引用不复述。