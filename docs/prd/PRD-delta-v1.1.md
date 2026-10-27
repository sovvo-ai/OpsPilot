# PRD Delta · v1.1（检索主题一致性过滤器）

| 项 | 值 |
|---|---|
| 文档版本 | **v1.1-delta-3**（v1 = critic 方案；v2 = SPIKE 后换药；**v3 = 外部裁决落定，改名 + FB 赌注预注册**） |
| 状态 | **待外部审查（验收轮）**（实现方不得自行改为"已冻结" —— 计划书附录维护规则 4） |
| 日期 | 2026-10-27 |
| 上游 | `docs/prd/PRD.md` v1.8 §6.9 / §7 / §11 / §12；`docs/testing/v3_residual_notes.md`；`docs/architecture/adr/0002-topic-filter-over-critic.md` |
| 变更性质 | **收敛性迭代**：不新增用户可见能力，只收口一个已知缺陷 |
| 范围 | **全路径**启用检索主题一致性过滤；critic 推 V1.2 |

> **本迭代已改名**：原「Critic Agent」→ **「检索主题一致性过滤器」**。
> critic 不是失败，是被数据证明**不该现在当主角**（ADR-0002 §3），
> SPIKE 的接线/预算/schema 结论全部存档，供 V1.2 立项复用。
>
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

> **规则**：任一赌注不达标 → 功能开关回滚、PRD 记录败因，
> **不改赌注数字**。赌注在开工前锁定，不允许在拿到结果后调阈值。
>
> **最终命名（v1.1-delta-2，外部裁决 2 拍板）**：赌注编号统一为 **FB1–FB4**
> （FB = Filter Bet）。原 B1a/B1b/B2/B2b/B3/B4 是 SPIKE 阶段的临时编号，
> 保留在后文作为推导过程，**判定以本节 FB 表为准**。
>
> **单一开关**：`TOPIC_FILTER_ENABLED`（默认 off）。
> 原双开关里的 `CRITIC_ENABLED` **本迭代不实现**（critic 推 V1.2，见 ADR-0002）。

### 2.1 FB 赌注表（预注册，先写数字再动代码）

| # | 赌注 | 判定口径（机器可测） | 基线 | 达标线 |
|---|---|---|---|---|
| **FB1** | **诱饵集越界流出 ≤ 基线 10%** | critic 专项集的 `detect_citation_overreach` 命中总数；**跑 3 轮取均值** | 21 处 / 15 run（A 臂） | **≤ 2 处**（10%）。⚠️ 实测单次 1 处只能算**点估计**，判定必须用 3 轮均值 |
| **FB2** | **主集 Recall@5 on/off 差 = 0 且 MRR ≥ 基线** | 35 题主集，filter on/off **同次对照** | Recall 0.9259 / MRR 0.7728 | Recall **差 = 0**（严格）；MRR **≥ 0.7728**。⚠️ **不许拿"专集涨没涨"抵主集跌** |
| **FB3** | **延迟按配对法判定** | 30 题 × 3 轮 on/off **配对**（同题相邻跑）；逐题配对差值均值 ± 95% CI（bootstrap 10000 次） | 见 §2.3 | **配对差值均值 ≤ +500ms 且 P50/P95 方向一致**；否则写「延迟影响不可判定，按零声明处理」 |
| **FB4** | **误杀回归固化** | 测试套件是否包含：v1/v2 失败形态 + `q029` 阈值边界（1.5 必杀）+ `q017` 跨文档哨兵 | — | **全部存在且绿**。已实现：`test_topic_filter.py`（v1/v2 形态）+ `test_boundary_sentinels.py`（q017/q029） |

### 2.2 为什么 FB1 必须「3 轮取均值」

SPIKE 的 C 臂实测是 **1 处 / 15 run**。但：

- 越界是**生成侧**行为（模型随机性），单次 1 处可能是运气；
- 基线 21 处本身也是 15 run 的合计，不是确定值；
- 10% 的线（≤2）与点估计（1）之间只差 1 处 —— **单次跑无法区分"真达标"与"噪声"**。

→ **判定用 3 轮均值**，且报告里必须同时给三轮的原始值（不许只报均值）。

### 2.3 FB3 的方法学（外部裁决 3：换方法）

**废弃的方法**：各跑一次，比裸 P50 / 裸 P95。

**为什么废弃**：n=15 时出现 **P50 +594ms 而 P95 −1548ms** 的**方向打架**——
filter 是纯字符串匹配，理论增量 ≈0，两个方向相反的差值只能说明测到的是
**模型时延噪声**，不是过滤器影响。

**新方法（预注册）**：

```
30 题 × 3 轮，on/off 配对（同题相邻跑，抵消账号/时段漂移）
统计量：逐题配对差值的均值 ± 95% CI（bootstrap，10000 次重采样）
```

**达标线（事后不许改）**：

> **配对差值均值 ≤ +500ms 且 P50/P95 方向一致**

不达标 → 报告写 **"延迟影响不可判定，按零声明处理"**
（既不宣称零劣化，也不宣称劣化）。

### 2.4 SPIKE 阶段临时编号（保留推导过程，判定以 §2.1 为准）

| # | 赌注 | 判定口径（机器可测） | 基线值 | 达标线 |
|---|---|---|---|---|
| **B1a** | **机器回归线**：overreach 流出数 **≤ 基线 10%** | `detect_citation_overreach(final_text, citations, chunks)` 返回条目数，**同次对照跑内** filter on/off 比较 | A 臂 15 run 共 **21 处**（10/15 run 有越界） | **≤ 2 处**（基线的 10%）。SPIKE 实测 **1 处 = 4.8%** ✅ |
| **B1b** | **真收口证明**：人工复核越界句 = 0 且误剥离/误剔除句 = 0 | 专项集 `filter-on` 的：① 全部含 `[n]` 的句子；② **全部被 filter 剔除的 chunk**（确认剔除得对）—— 逐句/逐 chunk 人工复核 | v3 人工口径 5/5（仅 5 句） | **越界句 = 0** 且 **误剔除 = 0** |
| **B2** | A3b 支持率**不降** + **Recall@5 不降** | ① 同 20 条抽样口径；② 35 题主集 Recall@5（filter on/off 同次对照） | A3b = 1.000；Recall@5 = **0.9259** | ① ≥ 基线；② **≥ 0.9259 且 ≥ 0.75（A2 门槛）**。SPIKE 实测 **0.9259（±0）** ✅ |
| **B2b** | MRR@5 **不降**（附加，filter 的顺带收益要量化） | 35 题主集 MRR@5 | 0.7728 | **≥ 0.7728**。SPIKE 实测 **0.8704（+0.0975）** ✅ |
| **B3** | kb_qa 主链延迟**零劣化** | **同次对照跑内** filter on/off 的 kb_qa P95 差值 | A 臂 P95 = 9875ms（n=15） | **on 不劣于 off**。⚠️ SPIKE 实测 P50 6781→7375（+594ms）、P95 9875→8327（−1548ms），**方向不一致，n 不足，需 30 题×3 轮重测** |
| **B4** | troubleshoot P95 **仍 ≤ 25s** | 同 `rag-eval-spec.md:90` 口径 | v3 实测 | **≤ 25000ms**。critic 不启用 → 此项**自动满足**（filter 零 LLM） |

### 2.5 ⚠️ 已知限制（必须对外写明，缺了就是隐瞒）

> **过滤参数以 5 篇语料 / 35 题标定，新语料泛化未验证，上线新文档集前须重扫阈值。**
>
> 这是**事实陈述，不是自我批评**：`min_consistency = 1.0` 是在本语料上扫出来的最优值，
> 阈值扫描显示 1.0 与 1.5 之间只隔着一道边界（`q029` 的 gold chunk 分数**恰好 = 1.00**），
> 这个边界的宽窄**完全取决于语料**。换文档集，1.0 可能过严也可能过松。

**必写位置**：README「已知限制」节、`v1.1-eval-report.md`、ADR-0002 §6.2。
**重扫方法**：`scripts/spike_v11/s5b_threshold_scan.py`。
**机制守卫**：`test_boundary_sentinels.py::TestOverfitGuard`（默认阈值/开关/文档三者被锁定）。

**为什么这条非写不可**：不写，将来换语料后 Recall 崩了，**没人知道是阈值问题**——
只会去查检索、查模型、查 chunk，查三天也查不到一个从未被记录过的假设。

### 2.0 ⚠️ 赌注表已按 SPIKE 结论重写（v1.1-delta-2）

原表围绕 **critic** 设计（B1 = "critic 后 overreach = 0"）。SPIKE 证明 critic 覆盖不到缺陷路径后，
赌注改为**围绕 filter** 设计，并把 filter 的副作用（Recall/MRR/延迟）全部纳入赌注 ——
**不能只报越界变好，不报检索质量的代价。**

| 原赌注 | 现赌注 | 变化原因 |
|---|---|---|
| B1「critic 后 detector = 0」 | **B1a「filter 后 ≤ 基线 10%」** | critic 对 kb_qa 零覆盖；filter 实测残留 1 处，写 0 就是撒谎 |
| B1b「人工复核」 | B1b 保留，但**对照物改为"被 filter 剔除的 chunk"** | 主药换了，误伤风险从"误剥离句子"变成"误剔除 chunk" |
| B2「A3b 不降」 | **B2「A3b + Recall@5 都不降」** | filter 动了检索集，**Recall 是首要风险**（实测第一版掉到 0.7407） |
| — | **B2b「MRR@5 不降」（新增）** | filter 的顺带收益，量化后才敢写进 README |
| B3「kb_qa 零劣化」 | 保留，但**基线改为同次对照** | 跨跑数字受时延漂移干扰（小项 1 已修正） |
| B4「troubleshoot P95 ≤25s」 | 保留 | 若 critic 不启用则自动满足 |

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

### 3.1 启用范围（**SPIKE 实测勘误，v1.1-delta-2**）

#### 3.1.1 ⚠️ 致命勘误：原范围设计与缺陷所在路径**错开**

**原设计**（v1.1-delta-1）：

| 条件 | 说明 |
|---|---|
| `route = troubleshooting` | 故障排查路径 |
| **超长答案**（`len(final_text) > 800 字`） | 长答案更可能夹带片段外内容 |

**两条都错了**，实测证据（`spike-report-v11-critic-phase2.md` §1）：

| # | 原假设 | 实测 | 证据 |
|---|---|---|---|
| 1 | 缺陷主要在 troubleshtooting 路径 | **错**：越界原型 m001 及全部"探针配置"类问题走 `kb_qa` | `chk_route.py`：m001 原题 / 对照单轮 / 探针问题 → 全部 `route=kb_qa, conf=1.0` |
| 2 | 超长答案可作为启用条件 | **错**：D1 挪位后 critic 在 `final` **之前**，此时 `final_text` 不存在，长度无法判断 | `prove_b.py`：kb_qa 的 `node_path` 无 troubleshoot，`evidence` 为 `method=skip` |

**错开的后果**：

> **要治的病住在 `kb_qa` 路径上，而 critic 只能治 `troubleshooting` 路径的病。**
> B1 赌注在专项集上大概率无法兑现（专项集诱饵题按 m001 型设计，全走 kb_qa）。

#### 3.1.2 修正后的范围（当前生效）

| 方案 | 启用范围 | 定位 |
|---|---|---|
| **filter（主题一致性）** | **全路径**（`kb_qa` + `troubleshooting`） | **主药**（治主因：污染 chunk 进引用池） |
| critic（逐句审查） | **仅 `troubleshooting`**（移除"超长答案"条件，理由见 §3.1.1） | 次药（治次因：模型挑错编号） |

**`kb_qa` 路径的越界由 filter 负责** —— 这是**写死的分工**，不是"口头带过"。
`kb_qa` 路径**不启用 critic**（无 draft、超时墙钟无余量，双重不可行）。

> **是否保留 critic、以何种范围保留，由三臂数据裁定**（见 §3.1.3）。

#### 3.1.3 三臂对照实测结果（决定主药的依据）

| 臂 | 配置 | 有越界 run | 越界总数 | Recall@5 | MRR@5 | P50 | P95 |
|---|---|---|---|---|---|---|---|
| **A** | critic off / filter off | 10/15 (67%) | **21** | 0.9259 | 0.7728 | 6781ms | 9875ms |
| **B** | critic on | **结构无效**（kb_qa 无 draft） | — | — | — | — | — |
| **C** | **filter on** | **1/15 (7%)** | **1** | **0.9259（±0）** | **0.8704（+0.0975）** | 7375ms | 8327ms |

**结论：filter 越界降 95%，Recall 零损失、MRR 反升、零 LLM 成本、全路径生效。**
**critic 对缺陷所在路径零覆盖。**

> ⚠️ **filter 不是万能**：仍有 1/15 残留（正确片段内部本身不支撑结论时，模型仍会编）。
> 但这不构成 critic 的正当性 —— critic 也覆盖不到那条路径。

### 3.2 `kb_qa` 路径：**不启用 critic，但由 filter 全覆盖**

> ⚠️ **本节已按裁决 4 重写**（原写"不启用 filter/critic"，与最终范围矛盾）。

| 方案 | `kb_qa` 路径 | 理由 |
|---|---|---|
| **filter** | ✅ **启用（全路径）** | 零 LLM、零超时增量；实测全路径 Recall ±0（FB2 已过） |
| **critic** | ❌ **不启用** | ① 该路径**无 `draft_answer`**（`prove_b` 实测 `evidence=method=skip`），critic 无输入可审；② 延迟仅余 ~200ms，加一次调用必破 8s；③ 预算已 5/6 |

**关键**：缺陷原型 m001 走的就是 `kb_qa`（`chk_route` 实测）。
**filter 全路径生效正是为了覆盖这条路径** —— 这是换主药的核心原因。

> 若将来要给 `kb_qa` 也上 critic，须先解决"先出草稿再审查"的结构性改造（V1.2/V1.3），
> 并单独立一个延迟赌注。**不允许在本迭代偷偷放进来。**

> **这是"不做"的决定，不是"暂缓"**。若将来要给 `kb_qa` 也上 critic，必须先在 FB3 之外
> 单独立一个延迟赌注 —— 不允许在本迭代偷偷放进来。

### 3.3 明确不做（越界即缺陷）

> ⚠️ **已按裁决修正**：原表里"检索层主题过滤"被列为"不做"，
> 但它在裁决 1 后**已成为本迭代主药**，故移除并改为"本迭代范围"。

| 不做 | 归属 |
|---|---|
| **critic 节点**（生成后逐句审查） | **V1.2**（立项证据见 ADR-0002 §4） |
| subgraph / 独立审查子图 | V1.2 |
| HITL（人工介入审查） | V1.3 |
| 新增评测**主集**题目（35 题基线**不动**） | 本迭代纪律（诱饵集单独建，不计入主集） |
| **rewrite 后二次检索收敛** | V2-2（`v3_residual_notes.md` 残留 2，会影响 Recall，需单独评估） |
| 自动改写答案内容 | 本迭代不做任何"重写"（filter 只剔除 chunk，不改答案） |
| 领域词表自动归纳 / 阈值自动标定 | V2（当前手工维护，`topic_filter.py` 内 `DOMAIN_TERMS`） |

#### 3.3.1 顺带收口的展示瑕疵（小项 3）

`a3b_user_review_v3.md` q029 的判定备注记录了这处瑕疵：

> 残留小瑕疵：清单生成器仍按位置标"引用 1"而正文角标是 [3]，
> 契约要求按 index 展示——清单脚本待改（不改变本条判定）。

**清理范围（本迭代实际需要做的）**：

| # | 修复项 | 状态 | 说明 |
|---|---|---|---|
| S1 | `run_steps` 新增 `agent_id` 列（迁移 0004） | ⬜ **推迟 V1.2** | critic 才有"按 agent 分账"需求；filter 不进图，无 agent 概念 |
| S2 | 前端轨迹抽屉泳道 / agent 标注 | ⬜ **推迟 V1.2** | 同上 |
| **S3** | **脚本按 `citations[].index` 展示，不用数组位置**（q029 瑕疵） | ⬜ **本迭代做** | 与 filter 无关，但属已记录的瑕疵，顺手清 |
| **S4** | **filter 剔除明细进 trace**（哪个 chunk 被哪个锚点拒） | ✅ **已实现** | `state.topic_filter` + `result["topic_filter"]` + step 摘要 |

> **S1/S2 推迟的理由**：它们是 `critic` 的配套（按 agent 分账、泳道展示）。
> 主药换成 filter 后不需要 —— 强做等于为未实现的功能建表。
> **本条属范围勘误，必须显式记录**（避免"任务书里有、代码里没有"的静默漂移）。
| S2 | 前端轨迹抽屉加泳道 / agent 标注（UI 最小改动） | `backend/internal/web/static/` |
| **S3** | **清单/脚本按 `citations[].index` 展示，不用数组位置**（q029 瑕疵） | `scripts/a3b_*` 相关脚本 |
| S4 | critic 的三态处置在 trace 中可见（含"疑似错位引用"提示） | `run_steps` + 前端 |

> S3 的口径与 `chat.py` 的 S9 修正（按 index 升序 + 显式 index 字段）一致 —— 前端已改，
> **脚本侧未改**，属同一契约的另一半。

### 3.4 越界诱饵集（`critic_golden.jsonl`，独立于主集）

> **改名说明**：SPIKE 时期叫"critic 专项集"，因当时主药是 critic。
> 主药换为 filter 后，它**不再服务于 critic**，而是**检验 filter 是否真能压住越界**。
> 文件名沿用 `critic_golden.jsonl`（历史一致，避免改动脚本引用），**内容是诱饵题**。

| 项 | 规定 |
|---|---|
| 规模 | **8 题**（已建成） |
| 文件 | `ai-service/evals/datasets/critic_golden.jsonl` |
| 标注 | 每题 `overreach_sensitive=true` + `bait_points` + `expected_verdicts` + `must_not_strip` |
| 用途 | **仅本迭代回归**（FB1 的测量集） |
| 是否进 README 七指标 | **否**。README 七指标口径不变；诱饵集结果单列在 `v1.1-eval-report.md` |

**已建成的 8 题**（每题都有真实依据，不是编的）：

| id | 题 | 诱饵类型 |
|---|---|---|
| b001 | livenessProbe / readinessProbe 怎么配 | `param_name` ×3（v3 实测原句命中） |
| b002 | 那它怎么配？（m001 原题） | `wrong_topic_doc` ×2 + `param_name` |
| b003 | Pod 探针参数怎么调 | `param_name` ×2 |
| b004 | proxy_read_timeout vs proxy_connect_timeout | `param_name` + **`synonym_note_reverse`**（反向诱饵） |
| b005 | Exit Code 137 代表什么 | `param_name`（数值易被补全） |
| b006 | connect() failed 110（**跨文档**） | `wrong_topic_doc` + `param_name` |
| b007 | MySQL 索引失效原因 | `param_name` ×2（易补优化手段） |
| b008 | CrashLoopBackOff vs ImagePullBackOff | `param_name` ×2 |

#### 3.4.1 必带 ground truth 与"越界诱饵点"

**没有 ground truth 的诱饵集，FB1/FB4 没有对照物** ——
人工复核会变成"看一句判一句"的自由裁量，无法回答"该抓的抓到了吗、不该删的删了吗"。

每题字段（实际文件已按此落地）：

```json
{
  "id": "b002",
  "question": "那它怎么配？",
  "overreach_sensitive": true,
  "turn_index": 1,
  "history": [{"role": "user", "content": "Pod 一直重启，怀疑是探针配置有问题，怎么查？"},
              {"role": "assistant", "content": "先看 describe 的 Last State 与 Events…"}],
  "gold_doc_ids": ["seed:k8s-pod-troubleshooting"],
  "bait_points": [
    {"type": "wrong_topic_doc", "token": "linux-network-troubleshooting",
     "why": "检索集稳定混入该无关文档（0.44 分），模型把它挂来支撑探针结论 —— v3 已实证"},
    {"type": "param_name", "token": "initialDelaySeconds",
     "why": "片段未给出该参数，模型易凭自身知识补上"}
  ],
  "expected_verdicts": [
    {"claim_contains": "startupProbe", "expect": "supported"},
    {"claim_contains": "livenessProbe 失败", "expect": "supported"}
  ],
  "must_not_strip": ["启动慢的应用应使用 startupProbe 保护",
                     "livenessProbe 失败会导致容器重启"]
}
```

| 字段 | 作用 |
|---|---|
| `bait_points` | **越界诱饵点** —— 标出"哪类参数名/哪类无关文档最易漏"。没有它，人工复核没有靶子 |
| `expected_verdicts` | 该句"应被保留"（`supported`）—— 服务的**不是 critic 准确率**，而是**filter 是否误杀正确内容** |
| `must_not_strip` | **误杀防线**（FB4）—— 列出"绝不能删的正确句" |

> ⚠️ **语义已随主药改变**：SPIKE 时期 `expected_verdicts` 用来算 critic 三态准确率；
> 现在主药是 filter，它**退化为一组"必须保留"断言** ——
> filter 若不慎把这些正确句依赖的 chunk 剔掉，就是误杀。

**诱饵点类型清单（至少覆盖 3 类）**：

1. `param_name` —— 片段未给出、模型易凭常识补的配置参数（如 `initialDelaySeconds`、`periodSeconds`）
2. `wrong_topic_doc` —— 检索集混入的无关文档被误挂引用（v3 已实证：`linux-network-troubleshooting`）
3. `synonym_note_reverse` —— **反向诱饵**（片段写"存活探针"、答案写 `livenessProbe`）：
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
---

## 8. 范围勘误（裁决 1 落定后，逐项对照原阶段 3 任务书）

> **为什么要单列**：原阶段 3 任务书是按"critic 当主药"写的。
> 裁决换成 filter 后，**部分条目已不适用**。逐条对照，避免"任务书里有、代码里没有"的静默漂移。

| 原任务书条目 | 现状 | 处置 |
|---|---|---|
| critic 节点实现 + 条件边 | ❌ 不做 | 推 V1.2（ADR-0002 §4） |
| `CRITIC_ENABLED` 功能开关 | ❌ 不做 | 改为 `TOPIC_FILTER_ENABLED`（已实现） |
| 预算按 SPIKE 结论落表 | ❌ 不做 | filter 不占预算（零 LLM/零超时）；critic 的 BUD-1d 方案留 V1.2 |
| 抢占优先级表更新（critic 档位） | ❌ 不做 | 同上 |
| `run_steps` 新增 `agent_id`（迁移 0004） | ⬜ 推迟 V1.2 | filter 不进图，无 agent 分账需求 |
| usage 按 agent 分账 | ⬜ 推迟 V1.2 | 同上 |
| 前端轨迹抽屉泳道/标注 | ⬜ 推迟 V1.2 | 同上 |
| `TestStateSchemaCompleteness` 扩为每 agent 白名单 | ✅ **已做** | `test_graph.py::TestStateSchemaCompleteness.AGENT_RETURN_KEYS`，含"越权写入即红" |
| critic 单测（三态/注入/不误伤） | ❌ 不做 | 推 V1.2 |
| 节点级回归（装配边界断言） | ✅ **已做（filter 版）** | `test_retrieval_node_return_keys_are_declared` 开/关两种 cfg 都测 |
| 所有中文用例走 httpx（G7） | ✅ 遵守 | 所有脚本用 httpx，无 Windows curl |
| 评测对照（on/off 全量） | ✅ 在做 | `s9_v11_eval.py`（FB1/FB2/FB4） |
| 延迟 30×3 配对 | ✅ 在做 | `s8_latency_paired.py`（FB3） |
| PRD 升版 + 版本回扫 | ⬜ 待验收通过后 | 阶段 5 收口 |
| CHANGELOG + tag v1.1 | ⬜ 待验收通过后 | **未达标不打 tag** |

### 8.1 残留 1/15 的回归清单（裁决 1 条件 ①）

> **它是 critic 未来的靶子，不是 filter 的 bug。** 但必须留一条可复查的记录。

| 项 | 内容 |
|---|---|
| 现象 | filter 后仍有 1/15 run 出现越界（`q001` 第 5 轮，SPIKE 实测） |
| 形态 | **正确片段（主题对）内部不支撑该结论**时，模型仍为该结论挂该片段的 `[n]` |
| filter 为何治不了 | filter 只判"chunk 属不属于本主题"，不判"这句话是否被该 chunk 支撑" |
| 本迭代处置 | **接受并记录**（ADR-0002 §4 逐字存档） |
| 复查方式 | 每轮评测检查 `v11_eval_raw.json` 的 `fb1.filter_rounds`；若均值 >2 需重新评估 |
| V1.2 靶子 | **critic 缩小范围后（troubleshooting 先行）**，赌注直接对这条写（ADR-0002 §4.1） |

> **对外表述底线**：可写「越界流出降 95%（21→1，三臂对照），残留 1/15 见已知限制」；
> **「根治」二字只能出现在否定句里**（如"未根治""不能宣称已根治"）。

---

## 9. 评测方法（本迭代验收的测量口径，预注册）

### 9.1 FB1（诱饵集越界）

- **数据**：`critic_golden.jsonl` 8 题
- **轮次**：**3 轮**，on/off 各跑
- **统计**：每轮的 overreach 总数；**判定用 3 轮均值**
- **报告要求**：必须同时给出三轮原始值与均值（**不许只报均值**）

### 9.2 FB2（主集检索指标）

- **数据**：`qa_golden.jsonl` 35 题
- **口径**：`hit_index` 按 `gold_chunk_texts` 子串匹配（与 `run_eval.py` 同源）
- **判定**：`ΔRecall = 0`（严格相等）**且** `MRR_on ≥ MRR_off`
- **附加要求**：必须列出"丢失命中"与"新增命中"的题号 —— **不许只报均值**

### 9.3 FB3（延迟）

- **方法**：30 题 × 3 轮，on/off **同题相邻跑**（配对）
- **统计**：逐题配对差值的**均值 ± 95% CI**（bootstrap 10000 次）
- **判定**：均值 ≤ +500ms **且** P50/P95 方向一致
- **不达标**：写「延迟影响不可判定，按零声明处理」

### 9.4 FB4（哨兵）

- **文件**：`test_topic_filter.py`（v1/v2 误杀形态）+ `test_boundary_sentinels.py`（q017/q029/过拟合守卫）
- **判定**：全部存在且 pytest 绿
- **实跑**：由 CI/本地 pytest 负责，评测脚本只检查存在性

---
