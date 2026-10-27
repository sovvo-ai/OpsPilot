# V1.1 SPIKE 实测报告（critic 审查者）

| 项 | 值 |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-10-27 |
| 状态 | **部分完成 —— 有阻塞项，不能进阶段 3** |
| 上游 | `docs/prd/PRD-delta-v1.1.md`、`docs/architecture/adr/0001-critic-agent.md` |
| git commit | `8402998`（阶段 1）+ 本次提交 |
| 环境 | Windows 10.0.26200 · Python 3.11.9（`ai-service/.venv`）· LangGraph 1.2.16 |

---

## 0. 通过/失败/阻塞 汇总

| # | 验证项 | 结果 |
|---|---|---|
| 1 | LangGraph 独立节点 + 条件边落位 | ✅ **通过**（含 1 个新发现，见 §1.4） |
| 2 | critic 模型档位（flash vs plus，JSON mode） | 🚫 **阻塞 —— 百炼账号欠费**（见 §2.1） |
| 3 | 预算集成 + 超时重算 | ✅ **通过**，但**结论推翻原方案**（见 §3.4） |
| 4 | detector → critic 输入对接成本 | ✅ **通过，零成本**（见 §4） |
| 5 | checker × 越界相关性统计（P2 补丁新增） | 🚫 **阻塞 —— 依赖 #2 的模型可用性**（见 §5） |
| 6 | critic 专项集 ground truth（P1 补丁新增） | 🚫 **阻塞 —— 需真实跑通才能标**（见 §6） |

> **判定：2/4 核心项通过，1 项得出推翻性结论，3 项被外部阻塞。**
> **按任务书纪律"这两样没做完不许进阶段 3"—— 现在不能进阶段 3。**

---

## 1. 验证项 1：LangGraph 独立节点 + 条件边

### 1.1 命令

```bash
cd ai-service && .venv/Scripts/python.exe /tmp/spike_lg3.py
```

（脚本内容见 §8 复现方式）

### 1.2 预期

在 `final` 之后插入 `critic` 节点，用 `add_conditional_edges` 按 `route` 与答案长度分流；
`kb_qa` 短答案应绕过 critic，`troubleshooting` 与超长答案应进入 critic。

### 1.3 实际

```
troubleshooting 短      critic_ran=True   text_len=6
kb_qa 短                critic_ran=False  text_len=1
kb_qa 超长(11字)         critic_ran=True   text_len=16
kb_qa 恰好10字           critic_ran=False  text_len=10
```

### 1.4 结论：**通过**，但踩到一个**第 4 次复发**的坑

**首次运行（脚本 v2）4 个 case 全部 `critic_ran=False`**，包括本应为 True 的超长 case。

**原因**：我用了 `seed` 字段传测试数据，但**没在 `TypedDict` 里声明 `seed`** ——
LangGraph 按 schema 过滤，**输入被静默丢弃**，`mk_final` 拿到空串，长度判据永远不成立。

**单独验证**（脚本 case4）：

```
未声明键 → 存在? False | 全部键: ['text']
```

**这是 `State` schema 过滤的第 4 次复发**：

| 次数 | 症状 | 发现处 |
|---|---|---|
| 1 | `retrieval_top_score` 未落库 | D2（S8） |
| 2 | `Citation.Index` 未声明 → 角标全"未找到引用" | S9 |
| 3 | `probe_chunk` 未声明 → 拒答拿不到最高分片段 | S9 |
| **4** | **SPIKE 脚本的 `seed` 未声明 → 条件边判据拿空串** | **本次** |

**对阶段 3 的直接要求**：critic 要写回 State 的**每一个**字段
（`critic_verdicts` / `critic_actions` / `critic_ran` 之类）
**必须先声明在 `State`**，且纳入 `TestStateSchemaCompleteness` 的每 agent 白名单。

> **API 形态结论**：`StateGraph.add_node` + `add_conditional_edges(源, 函数, {返回值: 目标})`
> 在 1.2.16 上工作正常，**无需 subgraph** —— 与任务书"不做 subgraph"的范围约束一致。
> critic 排在 `final` 之后时，需把 `final → END` 改为 `final → (条件) → critic | END`。

---

## 2. 验证项 2：critic 模型档位 —— 🚫 **阻塞**

### 2.1 阻塞原因（**不是代码问题，是账号问题**）

**命令**（最小连通性探测）：

```bash
cd ai-service && .venv/Scripts/python.exe /tmp/chk.py
```

**实际**（三个模型全部相同）：

```
qwen-flash         → 400 {"code":"Arrearage","message":"Access denied, please make sure
                            your account is in good standing...error-code#overdue-payment"}
qwen-plus          → 400 {"code":"Arrearage", ... 同上}
text-embedding-v4  → 400 {"code":"Arrearage", ... 同上}
```

**结论**：阿里云百炼账号**欠费/停用**（`Arrearage` = 欠款）。
所有模型（含 embedding）均不可用，**不是 critic 特有的问题，是整个项目的外部依赖中断**。

### 2.2 为什么这不是"SPIKE 失败"

失败的是**外部依赖**，不是技术验证。SPIKE 的目的是"验证可行性"，而
"模型调不通"本身是一个**必须上报的事实**，不是实现缺陷。

### 2.3 已完成的准备工作（待账号恢复即可跑）

`/tmp/spike_model2.py` 已写好并验证解析逻辑（含 markdown 围栏容错）：

- 3 个真实 critic 用例（supported / unsupported / needs_downgrade_label 各一）
- 每档模型 5 轮 × 3 用例 = 15 次调用
- 统计：JSON 合法率、三态判定正确率、耗时 min/median/max
- **从 `deploy/.env` 直接读 key**（实测 shell 环境变量里没有，直接读文件才拿到 116 字符）

### 2.4 待补的裁决（账号恢复后必须做）

| 待定 | 判定依据 |
|---|---|
| critic 用 `qwen-flash` 还是 `qwen-plus` | JSON 合法率 ≥95% 且三态正确率更高的那档；若 flash 达标优先 flash（快、便宜） |
| critic 的 `T_CRITIC` 超时值 | 取该档模型 median × 3（留余量），且必须满足 §3.4 的超时总和不超 60s |

---

## 3. 验证项 3：预算集成 + 超时重算

### 3.1 命令

```bash
python /tmp/spike_budget2.py
```

### 3.2 常量（来自 `budget.py:20-24` 与 `agent-workflow.md` §5.1，**逐项核对**）

```
MAX_LLM_CALLS = 6        RESERVED_FOR_FINAL = 1     WALL_CLOCK_MS = 60000
T_router=8000  T_rewrite=5000  T_retrieval=6000  T_troubleshoot=15000  T_checker=10000  T_final=15000
```

### 3.3 实际

```
① 现状（实测 1 次诊断）
   router + rewrite + troubleshoot + checker + final
   LLM=5/6   超时和=53000ms（余 7000ms）

② critic 在 final 之后
   LLM=6/6   超时和=61000ms  ← 超墙钟 1000ms ❌

③ critic 在 final 之前
   LLM=6/6   超时和=61000ms  ← 超墙钟 1000ms ❌

④ 若补上第 2 次诊断
   LLM=7/6（超 1）  超时和=76000ms（超 16000ms）❌❌
```

### 3.4 结论：**预算能过，超时不过 —— 必须重分配**

**两个约束分开看，结论完全不同**：

| 约束 | 现状 | 加 critic | 判定 |
|---|---|---|---|
| **LLM 额度** `MAX_LLM_CALLS=6` | 5 | 6 | ✅ **恰好够**（非 final 可用 5 次，实需 5 次，零余量） |
| **超时总和** ≤ 60000ms | 53000ms | **61000ms** | ❌ **超 1000ms** |

**关键洞察**：`critic` 排 `final` 前还是后，对**超时总和毫无影响**（都是 61000ms），
只影响 `reserved_for_final` 的语义。

> **`reserved_for_final=1` 的不变量要求 final 永不被抢。**
> critic 必须排在 `final` **之前**，从"非 final 的 5 次额度"里拿 1 次（4+1=5，恰好用完）。
> 若排在 `final` 之后，final 已消耗预留额度，critic 将无额度可取。

### 3.5 可选方案（**待账号恢复后按实测耗时定，现在只能给候选**）

| 方案 | 改动 | 总计 | 评价 |
|---|---|---|---|
| **BUD-1a** | `critic` 超时设 **7000ms**，其余不动 | 60000ms | ✅ 恰好卡线，**零余量**，风险高 |
| **BUD-1b** | `critic` **7000ms** + `rewrite` 降到 **4000ms** | 59000ms | ✅ 有余量 1000ms，**推荐**（rewrite 是轻量改写，4s 够） |
| **BUD-1c** | `critic` **6000ms** + `checker` 降到 **8000ms** | 57000ms | ✅ 余量最大，但 checker 降 2s 可能误判 |

> **不能选 BUD-2（critic 不计入 MAX_LLM_CALLS）** —— 那会破坏预算单一权威，
> 且 `reserved_for_final` 的语义将无法表达"critic 也不能抢 final 的额度"。
> BUD-3 已作废（见 delta §4.3.1）。
>
> **因此：BUD-1 系列是唯一可行方向。** 具体选 a/b/c 取决于 critic 实测耗时（依赖 §2）。

### 3.6 对赌注 B4 的直接影响

critic 增加一次 LLM 往返。若 critic 用 qwen-flash（预计 1~3s），
troubleshoot 路径 P95 会增加约 1~3s。当前基线需从 v3-final 报告读取后确认余量 ——
**该数字待账号恢复后实测**。

---

## 4. 验证项 4：detector → critic 输入对接

### 4.1 命令

```bash
cd ai-service && .venv/Scripts/python.exe /tmp/spike_adapter.py
```

### 4.2 实际

**① detector 输出**（构造 v3 的 m001 病灶）：

```json
[
  {"mark": 2, "chunk_id": "c-net",
   "sentence": "另外 initialDelaySeconds 建议设置为大于应用启动耗时[2]。",
   "tokens_not_in_source": ["initialDelaySeconds"]},
  {"mark": 2, "chunk_id": "c-net",
   "sentence": "livenessProbe 失败会导致容器重启[2]。",
   "tokens_not_in_source": ["livenessProbe"]}
]
```

**② 切句**（`_SENTENCE_SPLIT`）：

```
[0] Pod 一直重启通常是探针配置问题。
[1] 启动慢的应用应改用 startupProbe 提供启动宽限期[1]。
[2] 另外 initialDelaySeconds 建议设置为大于应用启动耗时[2]。
[3] livenessProbe 失败会导致容器重启[2]。
```

**③ 能否直接构造 critic 输入**：

```
claim[1]: 启动慢…[1]  marks=[1]  → pairs 可构造=True  pairs数=1
claim[2]: initialDelaySeconds…[2]  marks=[2]  → pairs 可构造=True  pairs数=1
claim[3]: livenessProbe…[2]  marks=[2]  → pairs 可构造=True  pairs数=1
```

### 4.3 结论：**通过，对接成本 = 零**

| critic 需要 | 现成来源 | 改造量 |
|---|---|---|
| `claim`（单句） | `_SENTENCE_SPLIT.split(final_text)` | **0**（复用现有常量，符合 P3 硬约束） |
| `mark`（声明编号） | `re.findall(REF_MARK_RE, sent)` | **0**（现成正则） |
| `pairs[].source`（片段完整原文） | `chunks[].content` | **0**（`state["chunks"]` 已含全文） |
| `pairs[].doc_title` | `chunks[].doc_title` | **0** |

**无需新增任何数据结构或查询。**

### 4.4 顺带实证：detector 的判定与 B1b 的对照关系

注意 `claim[1]`（startupProbe）**detector 未命中**，它确实是片段原句 —— 应判 `supported`。
这正是 B1b 的"**误剥离防线**"：critic 若把 claim[1] 判 `unsupported` 并剥离，就是误伤。

> 说明：**detector 未命中 ≠ 该句一定干净**（它只查技术标识符）；
> 但 **detector 命中的句子几乎必有问题**（本次 2 条命中经人工判断均为真越界）。
> 这支持"用 detector 作为 critic 的**廉价前置过滤器**"（ADR §2.3 已述）。

---

## 5. 验证项 5：checker × 越界相关性统计（P2 补丁）—— 🚫 **阻塞**

### 5.1 为什么做不了

需要**真实跑 35 题 + 专项集**，统计 `checker` 命中/未命中两类 run 的越界流出率。
**模型全部欠费，无法产生任何 run。**

### 5.2 已完成的准备工作

统计口径已定义（可脚本化）：

```
对每个 run 记录：checker_used(bool)  ×  overreach_count(int)
分组比较：
  - checker 命中组（checker 已调用）：n1 个 run，越界流出率 = Σoverreach / n1
  - checker 未命中组：n2 个 run，越界流出率 = Σoverreach / n2
判定：若 rate(命中) > rate(未命中) → BUD-3 作废（互斥方向反了）
      若 rate(命中) ≤ rate(未命中) → 才考虑 BUD-3 反转（只在命中时跑 critic）
```

`checker` 是否调用的信号已在 trace 中可取：`evidence` step 的 `output_summary`
含 `method=llm`（调用）或 `method=rule`（未调用）—— 见 `evidence.py:128-136`。

### 5.3 结论

**未完成 → 不能裁决 BUD-3 → 不能进阶段 3。**

---

## 6. 验证项 6：critic 专项集 ground truth（P1 补丁）—— 🚫 **阻塞**

### 6.1 为什么做不了

`bait_points`（越界诱饵点）与 `expected_verdicts`（三态期望）必须**基于真实答案**标注。
现在无法生成答案，只能凭空编造 —— 那不叫 ground truth，叫幻想。

`must_not_strip`（绝不能删的正确句）同理，需要看到模型真实产出的句子。

### 6.2 已完成的骨架（3 条用例，可作种子）

已验证 3 类诱饵点各 1 例（见 §2.3 的 CASES）：

| 诱饵类型 | 用例 | 期望 |
|---|---|---|
| `param_name`（正向） | `initialDelaySeconds 建议…` + 片段只讲 startupProbe | `needs_downgrade_label` |
| `wrong_topic_doc`（正向） | `livenessProbe 失败会重启` + 片段是 Linux 网络排查 | `unsupported` |
| `synonym_note`（**反向**） | `启动慢应用用 startupProbe` + 片段原句 | `supported`（**不许剥离**） |

### 6.3 待补

| 待做 | 前置 |
|---|---|
| 扩到 5~8 题 | 模型可用 |
| 每题标 `bait_points` / `expected_verdicts` / `must_not_strip` | 看到真实答案 |
| 落盘 `ai-service/evals/datasets/critic_golden.jsonl` | 同上 |

---

## 7. 未解决风险

| # | 风险 | 影响 | 处置 |
|---|---|---|---|
| R1 | **百炼账号欠费**，所有模型+embedding 400 `Arrearage` | 阶段 2 的 §2/§5/§6 全部阻塞，阶段 4 评测也做不了 | **需充值后复跑**。这是外部依赖，不是技术缺陷 |
| R2 | 超时总和 61000ms > 60000ms 墙钟 | 墙钟必然打断最后一个节点 | 必须重分配（BUD-1a/b/c），**不能加超时了事** |
| R3 | LLM 额度零余量（6/6） | critic 极易被抢占，实际执行率可能很低 | 评测必须统计 `critic_ran` 真实比例；若过低需再谈预算 |
| R4 | `State` schema 过滤已第 4 次复发 | critic 的新字段若不声明会被静默丢弃 | 阶段 3 必须扩展 `TestStateSchemaCompleteness` 为每 agent 白名单 |
| R5 | 计划书规划的"第 2 次诊断"从未实现 | 若未来补上，LLM 7/6 + 超时 76000ms **双双爆掉** | 本迭代不补；已在 delta §4.2 记录 |

---

## 8. 复现方式

```bash
cd /e/学习内容/OpsPilot/ai-service

# 项 1：LangGraph 条件节点（无需网络）
.venv/Scripts/python.exe /tmp/spike_lg3.py

# 项 2：模型档位（需百炼账号可用）
.venv/Scripts/python.exe /tmp/spike_model2.py

# 项 3：预算与超时（无需网络）
python /tmp/spike_budget2.py

# 项 4：detector → critic 对接（无需网络）
.venv/Scripts/python.exe /tmp/spike_adapter.py

# 欠费诊断
.venv/Scripts/python.exe /tmp/chk.py
```

> 脚本目前放在 `/tmp`（临时验证用）。**是否纳入版本库**：
> 项 1/3/4 的脚本建议固化为 `ai-service/tests/` 下的测试用例（阶段 3 一并做），
> 项 2 的脚本建议移到 `scripts/spike_v11_critic_model.py`（可复跑）。

---

## 9. 裁决节（任务书要求：每个决定附实测数字）

| # | 决定 | 结论 | 数字依据 |
|---|---|---|---|
| **D1** | critic 的图位置 | **`final` 之前**（`evidence → critic → final` 或 `troubleshoot → critic → final`） | `reserved_for_final=1` 不变量：critic 取"非 final 的 5 次额度"，4+1=5 恰好用完；若排 final 之后则无额度 |
| **D2** | 预算方案 | **BUD-1 系列（提额/重分配超时），BUD-2 与 BUD-3 排除** | LLM 6/6 恰好够（无需提额到 7）；超时 61000>60000 必须重分配；BUD-3 方向已证错（`evidence.py:104`） |
| **D3** | 超时具体分配 | **待定 —— 依赖 critic 实测耗时** | 候选：a) critic 7000 → 60000ms 卡线；b) critic 7000 + rewrite 4000 → 59000ms；c) critic 6000 + checker 8000 → 57000ms |
| **D4** | critic 模型档位 | **待定 —— 阻塞于账号欠费** | 判定标准：JSON 合法率 ≥95%，三态正确率更高者，flash 达标则优先 flash |
| **D5** | 是否启用条件边（route/长度分流） | **是** | 项 1 实测通过：4 个 case 分流全正确 |
| **D6** | detector → critic 对接方式 | **复用 `_SENTENCE_SPLIT` + 现有 `REF_MARK_RE` + `chunks[].content`** | 项 4 实测：改造量 0，pairs 可直接构造 |
| **D7** | 切分口径 | **强制复用 `chat.py:281`** | P3 硬约束；项 4 已验证可复用 |

---

## 10. 本阶段结论 + 下阶段风险

### 本阶段结论

**4 项中 2 项通过（项 1、4）、1 项得出推翻性结论（项 3）、1 项被外部阻塞（项 2）；
P2/P1 补丁新增的 2 项（项 5、6）同样被阻塞。**

**核心收获（推翻了两处原判断）**：

1. **超时墙钟会爆，不是预算会爆。** 原判断聚焦"LLM 调用次数"，实测发现
   `MAX_LLM_CALLS=6` 恰好够（5+1），真正的问题是**节点超时总和 61000ms > 60000ms**。
   这个约束之前**完全没被讨论过**。
2. **critic 必须排在 `final` 之前**，否则 `reserved_for_final` 不变量失效。
   这是 ADR 里没写清的（ADR §2.1 的图写作 `final ──► critic ──► END`，**位置错了**）。

### 下阶段风险（进阶段 3 前必须解决）

| # | 风险 | 阻塞程度 |
|---|---|---|
| 1 | **百炼账号欠费** | **硬阻塞** —— 项 2/5/6 全做不了，阶段 4 评测也做不了 |
| 2 | 超时重分配方案未定 | 中 —— 依赖 critic 实测耗时（即风险 1） |
| 3 | `T_CRITIC` 值无法确定 | 中 —— 同上 |

### 需要外部拍板的两件事

1. **百炼账号充值**（或换可用账号）—— 否则阶段 3 之后全部无法验证。
2. **ADR §2.1 的图需修正**：`final ──► critic ──► END` 应改为
   `critic` 在 `final` **之前**，否则与 `reserved_for_final` 不变量冲突。
   这是本次 SPIKE 发现的**文档缺陷**，我已在提交里同步修改 ADR，请复核。