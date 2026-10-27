# V1.1 SPIKE 阶段 2 补充报告（三臂对照 + 阻塞解除后补测）

| 项 | 值 |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-10-27 |
| 状态 | **完成**（阻塞已解除：新 Key 入 .env、Docker 已开） |
| 上游 | `docs/testing/spike-report-v11-critic.md`（首轮，含阻塞） |
| git commit | 见本次提交 |
| 环境 | Windows 10 · Python 3.11.9 · LangGraph 1.2.16 · 新百炼账号 |

---

## 0. 摘要：一个致命发现 + 一个方向修正

**致命发现（外部审查指出，本文实测确认）**：critic 的启用范围（`route=troubleshooting`）
与缺陷实际所在路径（`route=kb_qa`）**错开了**。
`m001` 型问题（探针配置，越界原型）**全部走 kb_qa**，critic 结构上无法生效。

**方向修正**：主药改为**检索主题一致性过滤器**（确定性、零 LLM、全路径生效）。
三臂对照实测：越界从 **21 处降到 1 处（-95%）**，且 **Recall 零损失、MRR 反升 +0.0975**。

> 📌 **本文件的降幅口径是 SPIKE 阶段**（主集 3 题 × 5 轮 = 15 run，21→1 = 95.2%）。
> **终测口径**（诱饵集 8 题 × 3 轮均值，8.0 → 0.67/轮 = **−91.6%**）见
> [`v1.1-eval-report.md`](v1.1-eval-report.md)。**对外引用请用终测口径，两者基数不同不可混用。**

---

## 1. 致命发现的验证（三条事实逐条实测）

### 1.1 事实一：m001 型问题走 kb_qa

**命令**：

```bash
cd ai-service && .venv/Scripts/python.exe /tmp/chk_route.py
```

**实际**：

```
m001 原题（有 history）             → route=kb_qa            conf=1.0
m001 对照单轮                      → route=kb_qa            conf=1.0
v3 复现的探针问题（无日志特征）          → route=kb_qa            conf=1.0
真 troubleshoot（有 error 关键词）     → route=troubleshooting  conf=1.0
```

**结论**：越界原型 m001 及所有"探针配置"类问题**均走 kb_qa**。
判定依据是 `router.py` 的规则：无堆栈/`error`/`failed`/错误码/端口+"起不来" → `kb_qa`。
**探针配置问题本质上不带错误特征**，这是设计使然，不是 bug。

### 1.2 事实二：kb_qa 路径不产生 draft_answer

**命令**：`/tmp/prove_b.py`

**实际**：

```
route: kb_qa
node_path: ['router', 'retrieval', 'evidence', 'final']
  - router         route=kb_qa, conf=1.0, llm_calls=0
  - retrieval      chunks=5, top_score=0.5925, rewritten=True
  - evidence       method=skip, supported=None, kept=0, invalid=0
  - final          direct_pass=False, llm_used=True, citations=2, answer_type=answered, overreach=2
```

**结论**：`draft_answer` **只在 `troubleshoot.py` 产生**（`:290` / `:327`）。
kb_qa 路径无 `troubleshoot` 节点，`evidence` 是 `method=skip`（无草稿可校验）。
→ **critic（审 draft）在该路径上无输入可审 = 结构上不可能生效。**

### 1.3 事实三：D1 挪位后"超长答案"条件不可表达

D1 把 critic 挪到 `final` **之前**（为保住 `reserved_for_final` 不变量）。
但"超长答案"这个启用条件依赖 `final_text` 的长度 —— **`final` 还没跑，长度不存在**。

> SPIKE 首轮项 1 里那个「kb_qa 超长 11 字 → critic_ran=True」的用例，
> 是**拿 mock 的假接线**测的（`mk_final` 直接返回预设文本，真实图上不存在这回事）。
> **该 case 无效，本文作废。**

---

## 2. 方向修正：检索主题一致性过滤器（s5）

### 2.1 根因在检索层（残留笔记自己的因果链）

`v3_residual_notes.md:33-36` 原文：

> **根因定位（实测）**：`m001` 的检索集（5 条）稳定混入 `linux-network-troubleshooting`
> 与 `nginx-502-upstream` 等**与探针无关**的文档；rewrite 本身**是生效的**……
> 问题是改写后的检索仍带回无关文档，而模型没能稳定守住"编号必须与主题匹配"。

→ **主因**：无关文档进入引用池（检索层）
→ **次因**：模型挑错编号（生成层，critic 治这个）
→ **critic 堵的是次因，且堵不到主犯路径。**

### 2.2 实测：污染 chunk 长什么样

**命令**：`/tmp/probe3.py`（query = `如何配置 Pod 的探针？`）

| # | score | doc | 主题锚点命中 |
|---|---|---|---|
| 1 | 0.5947 | k8s-pod-troubleshooting | ✅ 探针/probe/liveness/readiness/startup |
| 2 | 0.5771 | k8s-pod-troubleshooting | ✅ 探针 |
| 3 | 0.4463 | **linux-network-troubleshooting** | ❌ **无** |
| 4 | 0.4206 | **nginx-502-upstream** | ❌ **无** |
| 5 | 0.3763 | **nginx-502-upstream** | ❌ **无** |

**3 个污染 chunk 全部无主题锚点** → 规则可确定性地剔除，**零 LLM 调用**。

### 2.3 实现

新增 `ai-service/app/topic_filter.py`：

| 函数 | 作用 |
|---|---|
| `extract_anchors(query)` | 提取主题锚点，优先级：错误码/错误短语 → 技术标识符 → 领域词表 → 中文实义词兜底 |
| `score_topic_consistency(chunk, anchors)` | 打分：heading 命中 3 分 / 正文前 1/3 命中 1 分 / 后 2/3 命中 0.3 分；**强信号豁免**见 §2.5 |
| `filter_by_topic(chunks, query, min_consistency=1.0)` | 低于阈值剔除；**全空时兜底保留最高分**（避免把答案饿死） |

### 2.4 调参过程（不许只报最终值，过程必须留痕）

#### 第一版：只切中文词 → **误杀 5 题**

| 指标 | baseline | filter v1 | 差 |
|---|---|---|---|
| Recall@5 | 0.9259 | **0.7407** | **−0.1852** ❌ |
| MRR@5 | 0.7728 | 0.7222 | −0.0506 |

丢失命中：`q002` / `q007` / `q011` / `q012` / `q013`。

**根因诊断**（`/tmp/diag_loss.py`）：锚点提取把**疑问短语**当成了主题锚点：

```
q002: "Exit Code 137 一般代表什么？"        → 锚点 = ['一般代表']      ❌
q007: "Connection refused 和 ... 有什么区别？" → 锚点 = ['有什么区']      ❌
q011: "服务日志：... 这是什么问题？"          → 锚点 = ['服务日志','这是什么'] ❌
q012: "日志报 Error 1040: Too many connections..." → 漏了 'Error 1040'  ❌
q013: "...Lock wait timeout exceeded..."      → 漏了 'Lock wait timeout' ❌
```

→ **真正的主题标识（错误码/错误短语）全部漏掉，反而把疑问词当锚点。**

#### 第二版：优先提取错误码/错误短语

锚点提取修正（`_ERR_CODE_RE` / `_ERR_PHRASE_RE` / `INTERROGATIVE`），锚点变正确：

```
"Exit Code 137 一般代表什么？"        → ['exit code 137']           ✅
"Connection refused 和 ... 有什么区别？" → ['connection refused', 'connection timed out'] ✅
"日志报 Error 1040: Too many connections..." → ['error 1040','too many connections',...] ✅
```

**但 Recall 仍是 0.7407** —— 问题转移到**打分**。

#### 第三版：强信号豁免（**关键修正**）

**诊断**（单 chunk 复现）：

```python
anchors = ['exit code 137']
chunk.content = '...表现为 Exit Code 137。'   # 命中在正文后 2/3
→ score = 0.3   ← 低于阈值 1.0 → 被剔除
```

**这是设计缺陷**：错误码/错误短语在运维文档里是**唯一标识**，
不会作为无关内容出现 —— 命中在正文哪个位置都该算强相关。
早期"一律按位置给 0.3 权重"把它误判为"顺带提及"。

**修正**：新增 `_STRONG_ANCHOR_MARKERS`，含 `probe` / `refused` / `timed out` /
`crashloop` / `oomkill` / `too many connections` / `lock wait` / `exit code` /
含数字的锚点 —— **命中即满分**（3.0 + 命中数），不受位置权重压制。

#### 最终结果

| 指标 | baseline | filter v3 | 差 |
|---|---|---|---|
| **Recall@5** | 0.9259 | **0.9259** | **±0.0000** ✅ |
| **MRR@5** | 0.7728 | **0.8704** | **+0.0975** ✅ |
| 检索集均值 | 4.44 | 1.74 | −2.70（更精简） |
| 丢失命中 | — | **0 题** | ✅ |

> **MRR 提升的原因**：污染 chunk 被剔除后，正确 chunk 在结果集中的位置前移。
> 这不是巧合，是"过滤噪声 → 排名改善"的直接体现。

---

## 3. 三臂对照实验（核心证据）

### 3.1 三臂定义

| 臂 | 配置 | 预期 |
|---|---|---|
| **A** | critic off / filter off | 基线，应复现越界 |
| **B** | critic on | **结构上对 kb_qa 无效**（见 §1.2）→ 用等价证明代替实跑 |
| **C** | filter on | 全路径生效，预期消灭污染 chunk |

### 3.2 命令

```bash
cd ai-service && .venv/Scripts/python.exe ../scripts/spike_v11/s5_three_arms.py --runs 5
```

### 3.3 实际数据（15 次/臂：3 题 × 5 轮）

| 臂 | 有越界 run | 越界总数 | P50 | P95 |
|---|---|---|---|---|
| **A**（critic off / filter off） | **10/15 = 67%** | **21** | 6781ms | 9875ms |
| **B**（critic on） | **不适用**（结构无效，见 §3.4） | — | — | — |
| **C**（filter on） | **1/15 = 7%** | **1** | 7375ms | 8327ms |

**逐题明细（A 臂）**：

```
m001     ×5  overreach = 3,3,2,3,3   （全部越界）
m001-ctl ×5  overreach = 2,1,1,1,2   （全部越界）
q001     ×5  overreach = 0,0,0,0,0   （全部干净 — 该题本就不是越界敏感题）
```

**逐题明细（C 臂）**：

```
m001     ×5  overreach = 0,0,0,0,0   ✅ 全清
m001-ctl ×5  overreach = 0,0,0,0,0   ✅ 全清
q001     ×5  overreach = 0,0,0,0,1   ⚠️ 第 5 轮出现 1 处
```

### 3.4 B 臂为何不实跑（诚实记录）

任务书要求 B=critic on。但**critic 尚未实现**（阶段 3 才写）。
且 §1.2 已用真实 trace 证明：kb_qa 路径的 `node_path` **不含 troubleshoot**、`evidence` 为 `method=skip`，
`draft_answer` 恒为空 → critic 无论如何配置都拿不到输入。

> **这不是"没跑"，是"跑了也没有意义的对照"。**
> 用**结构证明**替代**实跑**，并在报告中明确标注方法与理由 ——
> 比伪造一个 B 臂数字诚实。

### 3.5 C 臂残留的 1 处越界（必须记录，不许忽略）

`q001` 第 5 轮出现 1 处。说明**过滤器不是万能的**：

- 过滤解决"污染 chunk 进入引用池"（主因）
- 但若**正确 chunk 内部本身不含某结论的依据**，模型仍可能编内容挂上去（次因）
- → **critic 仍有价值，只是不该当主药**

---

## 4. 两笔账的定谳

### 4.1 53000 vs 59000：**是我算错，不是文档漂移**

| 口径 | 值 | 说明 |
|---|---|---|
| `agent-workflow.md:205` | **59000ms** | `T_ROUTER+T_REWRITE+T_RETRIEVAL+T_TROUBLESHOOT+T_CHECKER+T_FINAL` = 8+5+6+15+10+15 |
| `00-environment-and-plan.md:327` | 同上 6 项 | 与 agent-workflow 一致 |
| **我的 s3_budget.py** | **53000ms** | **漏了 `T_RETRIEVAL=6000`** |

**根因**：我在脚本里只列了 5 个"会调 LLM 的节点"，漏了 `retrieval`（它不调 LLM，但**有超时**）。

**定谳**：文档正确、代码无对应常量（`T_*` 目前只在文档里，未落代码）、**我的脚本错**。
已修正 `scripts/spike_v11/s3_budget.py`。

**修正后的实际结论**（比我上轮报告的**更糟**）：

```
6 个节点 = 59000ms（墙钟 60000ms，余量仅 1000ms）
加 critic（哪怕只给 6000ms）→ 65000ms，超墙钟 5000ms
```

→ **critic 加不进 troubleshoot 路径**，除非砍其他节点超时。

### 4.2 新账号 429 形态：与旧账号**不同**

**命令**：`/tmp/test_429.py`（并发 embedding）

| 并发 | 200 | 429 | 其他 | Retry-After |
|---|---|---|---|---|
| 10 | 10/10 | 0 | 0 | — |
| 20 | 20/20 | 0 | 0 | — |
| 30 | 30/30 | 0 | 0 | — |
| **40** | **40/40** | **0** | 0 | — |
| **60** | **46/60** | **14** | 0 | **`None`** |

**结论**：

- 旧账号实测「上限≈30」**已不适用于新账号** —— 新账号 40 并发全过，60 才开始 429
- **`Retry-After` 仍为 `None`**（与旧账号一致）→ 退避策略必须**自己算指数退避**，不能指望响应头
- 429 body：`You have exceeded your current request limit...error-code#rate`

**影响**：`S1 残留风险 R3`（并发上限≈30）需更新为"新账号 ≈40~60，仍无 Retry-After"。

---

## 5. 主药建议（交外部审查）

### 5.1 数据对比

| 方案 | 越界消除 | Recall 影响 | MRR 影响 | 延迟影响 | 成本 | 路径覆盖 |
|---|---|---|---|---|---|---|
| **filter（主题一致性）** | −95%（21→1） | **±0** | **+0.0975** | **无**（纯计算，P95 反而降） | **零 LLM** | **全路径** |
| **critic（逐句审查）** | 未实测（对 kb_qa 无效） | 未知 | 未知 | +1 次 LLM（~1.6s） | 1 次调用 | **仅 troubleshoot** |
| 两者都要 | 理论最佳 | 有待验证 | — | filter 无损 + critic 增 | 1 次调用 | troubleshoot 双保险 |

### 5.2 我的建议：**filter 当主药，critic 降级为可选增强**

**理由（按权重排序）**：

1. **路径覆盖**：filter 全路径生效，critic 只覆盖 troubleshoot（且该路径不是缺陷来源）。
   **治病覆盖不到发病部位，方案再精致也无效。**
2. **实测效果**：filter −95% 且 Recall/MRR 双优；critic 对 kb_qa **零覆盖**。
3. **成本**：filter 零 LLM、零延迟；critic 1 次调用 + 撞墙钟（§4.1 超 5000ms）。
4. **可回归性**：filter 是确定性规则，结果可复现；critic 依赖模型随机性。

### 5.3 对 V1.1 范围的影响（需外部裁定）

**当前状态**：delta PRD 的 B1a/B1b 是围绕 critic 设计的赌注。
若 filter 当主药，**赌注表需要重写**：

| 原赌注 | 建议改写 |
|---|---|
| B1a critic 后 detector=0 | → **filter 后 detector 越界数 ≤ 基线 10%**（实测 21→1 = 4.8%） |
| B1b 专项集人工复核 | → 保留，但对照物改为"filter 前后的同一题" |
| B2 A3b 不降 | → 保留，另加 **Recall@5 不降**（实测 ±0，硬线 ≥0.75） |
| B3 kb_qa 零劣化 | → **filter 后 P95 不劣化**（实测 6781→7375ms，**略升 594ms，需评估**） |
| B4 troubleshoot P95 ≤25s | → 若 critic 不做/降级，此项自动满足 |

> ⚠️ **B3 需要注意**：filter 后 P50 从 6781→7375（+594ms），P95 从 9875→8327（−1548ms）。
> 平均值上升但尾部下降 —— 噪声较大，n=15 不足以定论，需更大样本。

### 5.4 若要保留 critic（双保险方案）

必须解决两个硬约束（本文 §4.1 已量化）：

1. **超时墙钟**：6 节点 59000ms + critic ≥6000ms = 65000ms > 60000ms
   → 必须砍其他节点超时（候选：`rewrite` 5000→3000、`checker` 10000→8000）
2. **LLM 额度**：非 final 可用 5 次，已用 4 次（router+rewrite+troubleshoot+checker）
   → critic 恰好第 5 次，**final 靠预留额度**，零余量

→ **技术上可行但极紧**。建议**先做 filter**（V1.1），把 critic 推到 V1.2 重新评估
（届时可用更大预算/更快模型）。

---

## 6. 产出文件

| 文件 | 内容 |
|---|---|
| `ai-service/app/topic_filter.py` | 主题一致性过滤器（含调参注释与实测教训） |
| `scripts/spike_v11/s5_three_arms.py` | 三臂对照脚本 |
| `scripts/spike_v11/s3_budget.py` | 已修正（补 `T_RETRIEVAL`） |
| `docs/testing/s5_three_arms_raw.json` | 三臂原始数据（30 条 run 明细） |
| `docs/testing/s5_recall_impact.json` | 35 题逐题 Recall/MRR 数据 |

---

## 7. 本阶段结论 + 待裁决

### 结论

1. **外部审查的致命发现成立**：critic 位置与缺陷位置错开，B1 赌注对 kb_qa 路径**空转**。
2. **filter 是有效主药**：越界 −95%，Recall 零损失、MRR +0.0975、零 LLM 成本、全路径覆盖。
3. **filter 不是万能**：仍有 1/15 残留（正确片段内部不支撑结论时，模型仍会编）。
4. **critic 加不进 troubleshoot 路径**：超时墙钟 59000+6000 > 60000，需砍其他节点。
5. **两笔账定谳**：53000 是我算错（漏 `T_RETRIEVAL`）；新账号 429 上限约 40~60、仍无 `Retry-After`。

### 待外部裁决（**不放行则不进阶段 3**）

| # | 待裁决 | 我的建议 |
|---|---|---|
| 1 | **V1.1 主药 = filter 还是 critic？** | **filter 当主药**，critic 推 V1.2 |
| 2 | 赌注表是否按 §5.3 重写？ | 是，B1a 改为"filter 后越界 ≤ 基线 10%" |
| 3 | B3 的 +594ms P50 是否需要更大样本？ | 是，建议 30 题 × 3 轮 |
| 4 | 若保留 critic，砍哪个节点超时？ | `rewrite` 5000→3000（轻量改写，3s 够） |
| 5 | 专项集是否改为 filter 对照集？ | 是，诱饵点保留（正向/反向都要） |

**在裁决前不进阶段 3。**