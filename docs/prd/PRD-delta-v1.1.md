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
| **B1a** | **机器回归线**：detector 命中 = 0 | `detect_citation_overreach(final_text, citations, chunks)` 返回条目数（对 `CRITIC_ENABLED=on` 的全部作答统计） | 5 次采样 `clean: 0/5`（每次 ≥1 处命中） | **0 处**，且不出现"删空答案"副作用。⚠️ **存在撤标盲区，单独不构成收口证明** |
| **B1b** | **真收口证明**：人工复核越界句 = 0 且误剥离句 = 0 | 专项集 `critic-on` 的：① **全部含 `[n]` 的句子**；② **全部被降级改标的句子** —— 两者合并后**逐句人工复核** | v3 人工口径 5/5（仅 5 句，覆盖面极窄） | **越界句 = 0** 且 **误剥离句 = 0** |
| **B2** | A3b 用户口径支持率**不降** | 同 20 条抽样口径（`a3b_user_review` 体系）+ 专项集 | v3 = 1.000（5/5，覆盖面有限，见 §5 R2） | **≥ 基线**，且被 critic 剥离的句子经人工复核确认"确实不该保留" |
| **B3** | kb_qa 主链延迟**零劣化** | **同次对照跑内** `CRITIC_ENABLED=on` vs `off` 的 kb_qa P95 差值（见 §2.2） | — | **on 不劣于 off**（同次跑内比较） |
| **B4** | troubleshoot P95 **仍 ≤ 25s** | 同 `rag-eval-spec.md:90` 口径（log_diag P95 ≤ 25000ms） | 见 `2026-10-24T1435_v3-final.json` | **≤ 25000ms** |

### 2.1 B1 为什么必须拆成 a/b 两行（P1 补丁）

**原 B1 有"自我裁判"漏洞。** 推理链：

1. detector（`detect_citation_overreach`）**只扫含 `[n]` 的句子**（`chat.py:311-314`：`if not marks: continue`）。
2. critic 的处置之一，恰恰是**撤掉 `[n]`**（`needs_downgrade_label` → 移除引用 + 追加「模型通用知识，无 KB 出处」）。
3. 于是：**所有可疑句都被降级改标后，detector 机械地报告 0 命中，B1 达标。**

> **但答案里那句话还在，只是不再挂引用了 —— 越界并没有被消除，只是被改了个标签。**
> 这是"**评测被自己机制 gamed**"的教科书案例。

**拆法**：

| 口径 | 能证明什么 | 不能证明什么 |
|---|---|---|
| **B1a（机器）** | 没有"带引用却无依据"的句子流出 | ❌ 不能证明"被撤标的句子本身没问题" —— 撤标即隐身 |
| **B1b（人工）** | 逐句复核：确实无越界、且没有误伤正确内容 | 依赖人工投入，不可每次 CI 跑 |

**两者并联才算收口**：B1a 是**可回归的回归线**（每次评测都跑），B1b 是**一次性真证明**（本迭代的验收依据）。
**只报 B1a = 0 命中就宣称"引用越界已根治"，属于口径游戏，禁止。**

### 2.2 B3 的双基线歧义（小项 1 修正）

原写法同时列了 7813ms（S9 方案A 简洁度纪律后的确认值，10 题单跑）与 6640ms（全量 35 题），
**两者不可直接比较** —— 跨跑数字受模型时延漂移影响（log_diag 上已领教过，见
`v3_residual_notes.md` 第 63-64 行的口径说明）。

**判定改为**：**同次对照跑内** `CRITIC_ENABLED=on` vs `off` 的对比（同 commit、同参数、背靠背跑）。
跨跑的历史数字（7813 / 6640）**只作参考背景，不作判定基线**。

理由：kb_qa 路径本就不启用 critic（§3.2），理论上差值为 0。
此赌注真正要验的是"**真的没被拖慢**"（例如条件边判断、state 拷贝等间接开销），
而同次对照是唯一能排除时延漂移的比法。

### 2.3 赌注的诚实性说明

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

**判定发生在 `final` 之前**（SPIKE 项 3 实测修正，见 ADR §1.1）：
critic 审查 `draft_answer` 并输出处置，结果通过 state 传给 `final`，
使 `final` 在生成时排除被剥离的句子 —— 这样**流式输出天然干净**（不会出现"已经发给用户又被撤回"）。

> ⚠️ 原写作"final 产出之后"，**与 `reserved_for_final` 不变量冲突**：
> 排在 final 之后的节点拿不到预留额度，必然被抢占。SPIKE 实测已修正。

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

#### 3.3.1 顺带收口的展示瑕疵（小项 3）

`a3b_user_review_v3.md` q029 的判定备注记录了这处瑕疵：

> 残留小瑕疵：清单生成器仍按位置标"引用 1"而正文角标是 [3]，
> 契约要求按 index 展示——清单脚本待改（不改变本条判定）。

**归入阶段 3 的 trace / 展示修复清单**，与 critic 的角标展示一并处理：

| # | 修复项 | 位置 |
|---|---|---|
| S1 | `run_steps` 新增 `agent_id` 列（迁移 **0004**） | `backend/migrations/` |
| S2 | 前端轨迹抽屉加泳道 / agent 标注（UI 最小改动） | `backend/internal/web/static/` |
| **S3** | **清单/脚本按 `citations[].index` 展示，不用数组位置**（q029 瑕疵） | `scripts/a3b_*` 相关脚本 |
| S4 | critic 的三态处置在 trace 中可见（含"疑似错位引用"提示） | `run_steps` + 前端 |

> S3 的口径与 `chat.py` 的 S9 修正（按 index 升序 + 显式 index 字段）一致 —— 前端已改，
> **脚本侧未改**，属同一契约的另一半。

### 3.4 critic 专项集（新增，独立于主集）

| 项 | 规定 |
|---|---|
| 规模 | **5~8 题** |
| 标注 | 每题标 `overreach_sensitive=true` + 期望行为 |
| 用途 | **仅本迭代回归** |
| 是否进 README 七指标 | **否**。README 的七指标口径不变，专集结果单列在 `v11-critic-eval-report.md` |

#### 3.4.1 必带 ground truth，且每题标"越界诱饵点"（P1 补丁）

**没有 ground truth 的专集，B1b 没有对照物** —— 人工复核会变成"看一句判一句"的自由裁量，
无法回答"该抓的抓到了吗、不该删的删了吗"。

每题的标注结构（阶段 2 产出，进 `ai-service/evals/datasets/critic_golden.jsonl`）：

```json
{
  "qid": "c001",
  "question": "Pod 一直 CrashLoopBackOff 应该怎么排查？",
  "route": "troubleshooting",
  "overreach_sensitive": true,
  "bait_points": [
    {"type": "param_name", "token": "initialDelaySeconds",
     "why": "片段未给出该参数，模型易凭自身知识补上并挂在探针片段引用下"},
    {"type": "wrong_topic_doc", "token": "linux-network-troubleshooting",
     "why": "检索集稳定混入该无关文档，易被误挂来支撑探针结论"}
  ],
  "expected_verdicts": [
    {"claim_contains": "startupProbe", "expect": "supported"},
    {"claim_contains": "initialDelaySeconds", "expect": "unsupported"}
  ],
  "must_not_strip": ["startupProbe 保护启动慢应用"]
}
```

| 字段 | 作用 |
|---|---|
| `bait_points` | **越界诱饵点** —— 明确标出"哪类参数名/哪类无关文档最容易漏"。没有它，人工复核就没有靶子 |
| `expected_verdicts` | 给 critic 的对照答案（三态），用于算 critic 自身的准确率 |
| `must_not_strip` | **误剥离防线** —— 列出"绝不能删的正确句"，直接服务 B1b 的"误剥离句 = 0" |

**诱饵点类型清单（至少覆盖 3 类）**：

1. `param_name` —— 片段未给出、模型易凭常识补的配置参数（如 `initialDelaySeconds`、`periodSeconds`）
2. `wrong_topic_doc` —— 检索集混入的无关文档被误挂引用（v3 已实证：`linux-network-troubleshooting`）
3. `synonym_note` —— **同义注记**（片段写"存活探针"、答案写 `livenessProbe`）：
   **这类必须判 `supported` 不许剥离** —— 它是误剥离的高发点（`a3b_user_review_v3.md` q001 已实证）

> 第 3 类是**反向诱饵**（考验"不误伤"），前两类是正向诱饵（考验"抓得住"）。B1b 要求两者同时为 0，
> 所以专集必须同时含正反两类，否则只能测出一半。

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

### 4.3 结论：LLM 额度恰好够，但**超时墙钟会爆**（见 §4.4）

`router(1) + rewrite(1) + troubleshoot(1) + checker(1) + critic(1) = 5`，
加 final 的预留额度 = **6 = MAX_LLM_CALLS，恰好用完，零余量**。

### 4.4 ⚠️ SPIKE 实测推翻：真正的瓶颈是**超时墙钟**，不是 LLM 额度

原分析只算了 LLM 调用次数，**漏了节点超时总和约束**。实测（`spike-report-v11-critic.md` §3）：

| 约束 | 现状 | 加 critic | 判定 |
|---|---|---|---|
| LLM 额度 `MAX_LLM_CALLS=6` | 5 | 6 | ✅ **恰好够**（非 final 可用 5 次，实需 5 次） |
| **节点超时总和 ≤ 60000ms** | 53000ms | **61000ms** | ❌ **超 1000ms** |

**这意味着**：即便 LLM 额度够，**墙钟也会在最后一个节点执行时打断它** ——
`T_final=15000ms` 排在末位，墙钟溢出 1000ms 意味着 final 可能被截断。

**必须重分配超时**。候选（待 critic 实测耗时定）：

| 方案 | 改动 | 总计 | 评价 |
|---|---|---|---|
| BUD-1a | `critic` 超时 **7000ms** | 60000ms | 恰好卡线，零余量 |
| **BUD-1b** | `critic` **7000ms** + `rewrite` 降到 **4000ms** | 59000ms | **推荐**（rewrite 是轻量改写，4s 够） |
| BUD-1c | `critic` **6000ms** + `checker` 降到 **8000ms** | 57000ms | 余量最大，但 checker 降 2s 可能误判 |

> **本项为硬约束**：`WALL_CLOCK_MS=60000`、`T_*` 的权威在 `agent-workflow.md` §5.1（单一权威）。
> 阶段 5 升版时，超时表的改动必须同步回该文件并回扫所有引用（维护规则 1/3）。

```
router(1) + rewrite(1) + troubleshoot(1) + checker(1) + final(1) + critic(1) = 6 = MAX_LLM_CALLS
```

**这意味着**：任何一处多调一次（模型重试、末来新增节点），就会触发 `preempted`。
且 `RESERVED_FOR_FINAL` 的语义要求 final 永不被抢 —— critic 若排在 final 之后调用，
它在抢占顺序中的位置必须**低于 final**，被抢占时**静默跳过并标 degraded**（不阻断答案）。

### 4.5 预算方案候选（BUD 编号）

**处置方向（待阶段 2 SPIKE 用实测数据定）**：

> ⚠️ **命名说明**：本节的预算方案用 `BUD-1/2/3` 编号，
> **不要与 ADR-0001 §2 的拓扑方案 `A/B/C`（独立审查者/自审/规则加强）混淆** —— 那是两套完全不同的决策。

- **BUD-1（提额）**：`MAX_LLM_CALLS` 提额到 7（需重算 `T_*` 超时总和 ≤60s）
- **BUD-2（独立配额）**：critic 不计入 `MAX_LLM_CALLS`（作为"后置质量层"独立配额）—— **需 ADR 论证是否破坏预算语义**
- ~~**BUD-3（与 checker 互斥）**~~：critic 只在 `checker` 未调用时启用 → **已作废，方向错误**

#### 4.5.1 BUD-3 作废的原因（P2 补丁）

原 BUD-3 的理由是"checker 已做过同类判断，被它放行的草稿边际收益低"。**该理由与代码事实相反**：

```python
# evidence.py:101
supported = bool(kept) and not invalid
# evidence.py:104
suspect = (not supported) or has_strong_claim(draft)   # ← 存疑才调 checker
```

**`checker` 被调用 = 这份草稿最可疑**（无有效引用 / 有无效引用 / 含强断言）。
互斥方案的实际效果是：

> **在最可疑的答案上取消逐句审查，在最干净的答案上保留逐句审查 —— 方向完全反了。**

**改为待实测裁决的候选**（不是直接删除，也不是直接采纳）：
SPIKE 增加一项统计 —— 35 题 + 专项集上，**`checker` 命中 vs 未命中两类 run 的越界流出率各是多少**。
若数据显示"`checker` 命中处的越界密度更高"（大概率如此），则 BUD-3 **要么反转（只在 checker 命中时跑 critic）要么作废**。

> **裁决必须用数据，不许用直觉**。若该统计因样本不足无法给出可信结论，则 BUD-3 **直接作废**，
> 优先级转向 BUD-1（提额 7 + 超时总和重算）。

> ⚠️ **本项若在 SPIKE 中证明不可行（无法在预算内完成），必须回到本文件修改赌注可达性，
> 标注"来源=实测"，不得直接进实现。**

---

## 5. 风险与已知不确定

| # | 风险 | 影响 | 处置 |
|---|---|---|---|
| R1 | critic 过度剥离导致 B2 下降 | 答案变空/变残 | 三态处置（`needs_downgrade_label` 而非删除）+ 剥离动作全进 trace 可复核 |
| R2 | B2 基线过弱（1.000 只覆盖 5 句） | "不降"可能是统计假象 | 专项集 5–8 题补充；报告必须同时呈现"抽样口径"与"全量机器口径" |
| R3 | 预算零余量 | critic 经常被抢占，实际上没跑 | SPIKE 定方案；报告必须记录 critic **实际执行率**（不是"配置为 on"就算跑过） |
| **R6** | **评测被自己机制 gamed**（P1 补丁新增） | 撤标即隐身 → B1a 假绿 | **拆 B1a/B1b 两口径**（§2.1）；B1b 人工复核必须覆盖"全部被降级句" |
| **R7** | **测量/处置口径不一致**（P3 补丁新增） | 切句不同 → 数字不可比 | critic **必须复用** `_SENTENCE_SPLIT`（ADR §3.1.1），加 `test_critic_claim_split_matches_detector` |
| **R8** | **错位引用被误判 unsupported** | 正确内容被剥离 | **不修**（输入最小化的必然代价）。critic reason 输出"疑似错位引用"进 trace，禁止自动改配（ADR §6.2.1） |
| R5 | 迁移编号冲突 | 迁移失败 | **用 `0004`**，不用任务书写的 `0003`（已被 `0003_retrieval_stats` 占用） |

---

## 6. 验收判定（本迭代）

| 判定 | 条件 |
|---|---|
| **PASS** | **B1a + B1b + B2 + B3 + B4 全部达标** + 门禁全绿（`go vet` / `go test` / `pytest`）+ 对照报告含 git commit 与逐题结果 |
| **PARTIAL** | 部分赌注达标 → 按 §2 规则回滚功能开关，报告写败因，**不得打 `v1.1` tag** |
| **FAIL** | **B1b 未达标**（人工复核仍有越界句或误剥离句）→ 回滚，PRD 记录，不进 README |

> **B1a = 0 但 B1b 未做或未达标 → 判 PARTIAL，不得判 PASS。**
> 机器口径单独不构成收口证明（§2.1），这是本轮迭代最重要的判定纪律。
>
> **「交付完成」≠「验收通过」** —— 与 S0–S9 同一口径。本迭代的最终结论由外部审查出具。

---

## 7. 与主文档的关系（升版时机）

本 delta 通过审查后，阶段 5 才把内容**合并进** `docs/prd/PRD.md`：

| delta 内容 | 合并到主文档位置 |
|---|---|
| critic 功能需求条款（FR 新增） | §6.9 工作流编排 |
| 三态处置的用户可见行为 | §7（新增"引用审查"小节） |
| B1a / B1b / B2 / B3 / B4 赌注 → 验收项 | §11.1（A16 `critic 引用审查`，含机器口径与人工口径两条） |
| 预算提额的常量变更 | §6.9 FR-36（若 SPIKE 裁决选 BUD-1） |
| 本迭代决策记录 | §14 修订记录（v1.10） |

**阶段 5 必须做的版本引用回扫**（维护规则 1）：`grep -rn "MAX_LLM_CALLS\|reserved_for_final\|6 次" docs/`，
确认所有引用位置同步 —— 常量为单一权威（`agent-workflow.md` §5.1），其余文档只引用不复述。