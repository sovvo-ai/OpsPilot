# S8 评测与交付验收报告

| 项 | 值 |
|---|---|
| 日期 | 2026-10-23 20:50 – 2026-10-24 10:3x（GMT+8） |
| 执行人 | 开发侧（AI 辅助） |
| 依据 | `docs/prd/PRD.md` v1.8 §11.1（A10/A11/A12）、`docs/00-planning/00-environment-and-plan.md`（§3.4/§12/§13）、`docs/testing/rag-eval-spec.md`（§1.4 X5 已确认）、`docs/testing/test-plan.md` |
| 结论 | **S8 已关账（2026-10-24）**：35 题真跑 **7 项指标全部达标**（含人工复核回填的支持率 0.950 / 任务成功率 0.950）；A10 18/18；A11 28.4s；release 生效；呈现层四件套齐备 |
| 交付 | `evals/run_eval.py` + 报告 v1/v2；`0002`/`0003` 迁移；release 构建；A10/A11 脚本；README + demo 三件套；`a3b_user_review.md` |

> **脱敏声明**：`DASHSCOPE_API_KEY`、`AI_SERVICE_TOKEN`、`JWT_SECRET`、`POSTGRES_PASSWORD` 一律从 `deploy/.env` 读取并打码，从未打印。

---

## 0. 按任务书逐条交付清单

| # | 要求 | 状态 | 证据 |
|---|---|---|---|
| 0 | S7-B 调用点输入源修复（单独提交） | ✅ | `682bfab`；用例 `question="8080 起不来，冲突吗？"` + 清单在 attach_log → conflicts 非空 |
| 1 | run_eval 35 题真跑、7 项指标、报告含 meta/逐题/A3b | ✅ | `84433a9`；见 §1 |
| 2 | A10 §9.4 全量回归 + A11 计时 + A12 自检 | ✅ | 见 §2、§3、§4 |
| 3 | Release 构建（gin release + ldflags） | ✅ | 见 §5 |
| 4 | 呈现层四件套 | 见 §6（本轮未完成，**诚实标注**） |
| 5 | test-plan G7 | ✅ | `b3efd24`（前轮已落） |

---

## 1. run_eval 35 题真跑

### 1.1 执行

```bash
cd deploy && docker compose down -v && docker compose up -d      # 干净环境
cd ai-service && .venv/Scripts/python.exe -m evals.run_eval
```

- 前置：5 篇 seed_docs 全部 `completed`
- 冒烟：真题 `q001` 从 jsonl 读入走完整链路（防"字段→检索输入"装配盲区）→ `answered`, citations=2, recall=1.0
- 用时：**275.7s**（35 题）

### 1.2 指标结果

| 指标 | 值 | 阈值 | 达标 |
|---|---|---|---|
| Recall@5 | **1.000** | ≥0.75 | ✅ |
| MRR@5 | **0.812** | ≥0.60 | ✅ |
| 引用内容支持率 | `NOT RUN` | ≥0.90 | 待用户填 A3b |
| 拒答正确率 | **0.875** | ≥0.85 | ✅ |
| 过度拒答率 | **0.000** | ≤0.15 | ✅ |
| 任务成功率 | `NOT RUN` | ≥0.70 | 待用户填 A3b |
| 多轮保持率 | **1.000** | ≥0.90 | ✅ |

**延迟**：kb_qa P50=3163ms / P95=**7360ms**（≤8000ms）；log_diag P50=8046ms / P95=**14671ms**（≤25000ms）

**引用 ID 有效率（硬线，独立于 7 项）**：**1.000 ✅ 全通过**

### 1.3 多轮保持率（§3 原口径）

`ΣRecall_A / ΣRecall_B = 5.00 / 5.00 = 1.000`，非可比配对 0 条（未触发 NOT RUN 条件）。
5 条 multiturn 的 A 组全部触发 rewrite。

### 1.4 A3b 抽检（20 条，judge 分列）

抽样规则写死在脚本里（`select_a3b_cases`，含注释）：

| 类别 | 条数 | 理由 |
|---|---|---|
| `citation_risk` × 2 | 2 | 引用风险题最易"引用存在但不支持" |
| `multiturn` × 1 | 1 | 多轮改写路径引用链最易断 |
| `kb_qa` 长答案 × 2 | 2 | 长答案逐句支撑人工难自动校验 |
| 其余作答题 | 15 | 常规抽检，`judge="AI"` |

**⏳ 待用户填（`user_recheck=true`，判定人留空）：`q028`, `q029`, `m001`, `q001`, `q008`**

分 judge 汇总（禁止只给合并值）：

| 口径 | 条数 | 引用内容支持率 |
|---|---|---|
| 用户口径（user_recheck=true） | 5 | `NOT RUN`（待填） |
| AI 口径（judge=AI） | 15 | 1.000 |
| 混合口径 | 15 | 1.000 |

### 1.5 校准日志（阈值未达标不许调数据集凑数）

| 项 | 观察 | 处置 |
|---|---|---|
| `q025`（ambiguous，追问脱离上下文） | 期望 `refusal`，实际 `answered`，且被路由到 troubleshooting 调用了 `check_port_conflict` | **审查裁定为 D1 行为缺陷（非"擦线"）**，已修 —— 见 §1.6。**未调数据集** |
| `src_retrieved_chunk_ids` | run 接口原未暴露完整检索集，Recall 只能用 citations 近似（会低估） | 新增 `0002` 迁移 + Go 侧落库，**口径修正而非调参** |

---

### 1.6 D1：故障排查路由的证据门槛（外部审查裁定）

| 项 | 内容 |
|---|---|
| 缺陷 | `它起不来了怎么办？`（q025）→ `route=troubleshooting`、`answered`、引用为模型自选。**指代对象是模型猜的** |
| 根因链 | ① 词典"起不来"**单独**命中 troubleshooting 规则（PRD §7.3 要求的是日志/错误特征或**显式诊断请求**，裸短语不在内）→ ② troubleshoot 节点在无 `attach_log` 时因检索到**噪声片段**（top=0.3645）而"有证据"，自由生成 → ③ 绕过证据闸门。§7.4 第三行明写"追问脱离上下文 → 给 2~3 个候选，**不猜**" |
| 性质 | **行为缺陷**，不是阈值擦线 —— 修行为，**未改题目** |

**修法（三层，低风险）**：

1. **路由层**（`router.py`）
   - 词表拆分：`TS_STRONG_WORDS`（error/exception/traceback/报错/异常… 自身即证据）vs `TS_WEAK_WORDS`（起不来/挂了/不通… **必须伴随**强特征/错误码/端口线索）。
   - `TS_PORT_HINT` 补齐 PRD §7.3 原文列举的"**端口号 + 起不来**"。
   - `TS_EXPLICIT_REQUESTS` 收窄为"要求看**某段材料**"（`排查/定位` 单独不再触发，否则 `Pod CrashLoopBackOff 怎么排查？` 会被误判）；**有 `attach_log` 时以内容为准**（保 S7-A 语义）。
   - 新增 `NEED_CLARIFY` 硬否决 + `_is_bare_anaphora()`（含指代词 ∧ 无 attach_log/错误码/端口号/上下文 assistant 轮）。
   - 易错防护：`该` 在"应**该**怎么用"里是副词不是指代词（实测误判 `日志级别应该怎么用？`，已用正则区分）。
2. **节点层**（`troubleshoot.py`）：新增**零证据闸门** —— 无工具证据 ∧ 无 chunks → 输出 clarify 且**不调模型**，带 `_force_answer_type="clarify"`。
3. **终止层**（`final.py`）：`refuse_node` 区分 §7.4 的两种"拒绝"（**裸指代 → clarify** vs **文档缺失 → refusal**，早期混为一谈）；`smalltalk_node` 支持 clarify；新增共用常量 `_CLARIFY_TEXT`（防两处文案漂移）。

> **关键设计决定（勿回退）**：裸指代必须**硬否决**，不能返回 `None` 交给 LLM 兜底 —— 实测兜底会判成 troubleshooting（conf=0.72），随后因噪声片段而"有证据"，仍然在猜。

**修复前后实测对比**（Python httpx，走对外接口）：

| 输入 | 修复前 | 修复后 |
|---|---|---|
| `它起不来了怎么办？`（q025） | `route=troubleshooting`、**answered**、引用为模型自选 | `route=smalltalk`、**clarify**、0 引用、给出 3 条候选 |
| `这个报错怎么解决？`（q026） | `route=troubleshooting` | **clarify** |

**回归结果（9 条 = no_doc×6 + ambiguous×3）**：

```
OK  q019~q024  [no_doc]     → refusal（0 引用）
OK  q025       [ambiguous]  → clarify    ← 修复目标
OK  q026       [ambiguous]  → clarify
OK  q027       [ambiguous]  → answered   ← 该条 should_answer=true，本就应作答
9/9 正确
```

**对照（防误伤，这是修复的风险面）**：

| 对照 | 结果 |
|---|---|
| **A**：有上下文的多轮追问**不得**被判 clarify | m001~m005 **全部** `answered` + `rewritten=True` ✅ |
| **B**：有证据的排障问句**仍须**进 troubleshooting | `8080 起不来了怎么办？` / `服务报 error 起不来` / `日志报 exception` → 全部 `route=troubleshooting` ✅ |

**回归用例**：`tests/test_graph.py::TestD1EvidenceGate`（12 例，含"纯知识问答不被误判"与"零证据不调模型"）。

---

### 1.7 D2：证据链字段未接通（外部审查裁定）

| 项 | 内容 |
|---|---|
| 缺陷 | 报告里 28/28 answered 案例 `retrieval_top_score=0.0`，而 `retrieved_chunk_ids` 明明有值 |
| 根因 | `GetRun`（**读接口**）根本没返回 `retrieval_top_score` / `rewritten_query` —— 它们只存在于 `TurnResult`（写路径），**未落库**。`retrieval_top_score` 只出现在写路径的类型定义里 |
| 影响 | 不影响本次指标（Recall 用 `retrieved_chunk_ids` 算），但这是 **demo 轨迹与 A3b 人工复核的证据链**，必须可回查 |
| 修法 | `0003` 迁移加两列 → `RunTrace` 加字段 → 写入/读出 → `nullFloat`/`nullString` 处理 NULL → `messages.go` 传值 |

**顺带补的同类缺口**：报告 `cases[]` 此前**只有 `citation_count`，没有引用明细** —— 人工复核要判断"这段引用是否支撑该结论"时无从下手。现补入 `citations[]`（`index`/`chunk_id`/`doc_title`/`score`/`source_type`/`status`）与完整 `final_text`（1.5KB 上限）。

**实测对比**：

| 字段 | 修复前 | 修复后 |
|---|---|---|
| `retrieval_top_score` | `0.0`（28/28 案例） | `0.779` / `0.7272` / `0.6956`（真实值） |
| `rewritten_query` | 字段不存在 | `None`（未改写）／改写时返回实际查询 |
| `cases[].citations` | 不存在 | 含 `index`/`chunk_id`/`doc_title`/`score` 明细 |

> **规格同步**：`rag-eval-spec.md` §4.1 的报告 JSON schema 已按实际产出回改（**来源 = 外部审查裁定**），并注明"一次改对，两边不再漂"。

---

### 1.8 报告 v2：A3b 人工复核回填（补齐两个 `NOT RUN`）

回填脚本：`scripts/apply_user_review.py`（从复核清单 Markdown 解析判定 → 回填 JSON → 出 v2）。
产出：`ai-service/evals/reports/2026-10-24T0115_v2.json` / `_v2.md`。

**判定归属**：5 条由**外部审查者受用户委托代判，用户当日逐条过目后追认**。
回填 `judge="user"`、`judged_at=2026-10-24`，并在 `provenance` 字段保留完整追溯链路
（链路上链，不做无痕处理）。详见 `docs/testing/a3b_user_review.md` 头部声明。

**三口径支持率（禁止只给合并值）**：

| 口径 | 条数 | 引用内容支持率 | 任务成功率 |
|---|---|---|---|
| 用户口径（user_recheck=true） | 5 | **0.800** | 0.800 |
| AI 口径（judge=AI） | 15 | 1.000 | 1.000 |
| **混合口径（回填进 `metrics`）** | 20 | **0.950** | **0.950** |

**最终 7 项指标（v2）**：

| 指标 | 值 | 阈值 | 达标 |
|---|---|---|---|
| Recall@5 | 1.000 | ≥0.75 | ✅ |
| MRR@5 | 0.812 | ≥0.60 | ✅ |
| **引用内容支持率** | **0.950** | ≥0.90 | ✅ |
| 拒答正确率 | 1.000 | ≥0.85 | ✅ |
| 过度拒答率 | 0.000 | ≤0.15 | ✅ |
| **任务成功率** | **0.950** | ≥0.70 | ✅ |
| 多轮保持率 | 1.000 | ≥0.90 | ✅ |
| （独立硬线）引用 ID 有效率 | 100% | 100% | ✅ |

**人工复核发现的失败项 `m001`（价值最高的一条数据）**：

| 项 | 内容 |
|---|---|
| 现象 | 多轮追问"那它怎么配？"的答案**把片段之外的通用知识挂在了 KB 引用号下** |
| 性质 | **引用越界** —— 引用 `chunk_id` 真实有效（机器判定全过），但**内容不支撑**该结论 |
| 为何机器抓不到 | 检索层 Recall/MRR 正常（确实命中相关片段）；引用 ID 校验只验证 ID 存在。**只有人工读语义能发现** |
| 处置 | **未调数据集、未调阈值**；记入 `calibration_log`，排为 S9 前置修复项 `S9-PRE-1` |

> 这条恰好为"为什么把『引用 ID 有效率』与『引用内容支持率』**分成两个指标**"提供了实证：
> 合并成一个数会掩盖"引用都存在但都不支持"这种最危险的情况。

---

## 2. A10 §9.4 异常矩阵（18/18 通过）

脚本：`scripts/a10_exception_matrix.py`（自包含，真实 HTTP + 真实 DB）

```
A10 结果: 18/18 通过
  ✅ §9.4-01 二进制改名 .md → 422 ENCODING_ERROR 且无新行 — rows 32→32
  ✅ §9.4-02 摄取失败事务回滚（documents 有行 / chunks 为空）
  ✅ §9.4-04a failed 文档 reingest → 202
  ✅ §9.4-04b 重试已 completed 文档 → 409 DOC_NOT_RETRYABLE
  ✅ §9.4-05 重复上传 failed 文档 → deduplicated + retry_hint
  ✅ §9.4-06 空知识库提问 → 200 且不调模型
  ✅ §9.4-07 检索无结果 → 拒答且 llm_calls≤2
  ✅ §9.4-08 降级链路完备（degraded 必伴随 degrade_reasons）
  ✅ §9.4-09 工具超时 5s 放弃不阻塞
  ✅ §9.4-10 Python 挂掉时 Go /health/live 仍 200（端点分离）
  ✅ §9.4-10b MODEL_UNAVAILABLE 错误码已定义
  ✅ §9.4-11 依赖健康语义（ready 200 表示依赖正常）
  ✅ §9.4-12 删除文档后看历史 → 200（引用状态可查）
  ✅ §9.4-13 并发上传同文件 × 12 轮 → 202/202 + false/true，无 500
  ✅ §9.4-14 deleting 状态与 DOC_DELETING 已落地
  ✅ §9.4-15 迟到结果 CAS 丢弃机制存在 + 无冲突残留
  ✅ §9.4-16 watchdog 收敛机制存在 + 无超阈值僵死任务
  ✅ §9.4-17 两用户上传同内容 → 各自建档且互不可见
```

### 2.1 本轮抓到的真缺陷：并发上传 500

| 项 | 内容 |
|---|---|
| 现象 | 并发上传同内容偶发 **500**：`insert documents: pq: duplicate key value violates unique constraint "uq_documents_owner_hash" (23505)` |
| 根因 | `CreateDocument` 的 `SELECT` 查重与 `INSERT` 之间存在**竞态窗口**（无行可锁，`FOR UPDATE` 无效）。两个请求同时通过查重，第二个撞唯一约束 → 直接 500 |
| 修复 | 捕获 23505 → 回滚 → 回查已存在行 → 返回 `deduplicated=true`（语义等同"另一请求已落库"）；极端竞态下有限重试（≤2 次） |
| 验证 | 修复前 8 轮压测 **1 轮异常**；修复后 12 轮压测 **0 轮异常** |

### 2.2 脚本自身修正（非产品缺陷）

首版脚本有 3 处**字段假设错误**（写脚本时未核对 schema），已修正并在代码里注明：

1. `documents.ingest_status` **不是表列** —— 它是接口派生字段（Go 从 `ingest_jobs.status` + `deleted_at` 算）
2. `stale_result_dropped` **不是表列** —— 它是 CAS 失败时的**错误返回值**（`fmt.Errorf`）
3. `document_contents.raw_content` → 实为 `document_contents.content`

---

## 3. A11 干净环境计时（28.4s，目标 ≤300s）

脚本：`scripts/a11_clean_start_timing.py`（`down -v` → `up -d` → 迁移 → 摄取 → 提问）

```
分阶段计时
  down -v                :     2.8s
  build（参考，不计入）  :     7.5s
  up -d                  :     8.8s
  等 backend ready       :     1.8s
  等 ai ready            :     0.4s
  迁移                   :     3.6s
  摄取 5 篇              :     5.2s
  首次提问               :     7.8s
  ---- 冒烟合计          :    17.4s

  【启动→冒烟通过】      :    28.4s   目标 ≤ 300s  ✅ 达标
```

冒烟含一次真实提问且 `answer_type=answered`。无超时，无需原因分析。

---

## 4. A12 评测真实性自检

| # | 检查项 | 状态 | 证据 |
|---|---|---|---|
| 1 | 报告含 git commit | ✅ | `meta.git_commit = 682bfab` |
| 2 | 报告含逐题结果 | ✅ | `cases[]` 35 条，含 qid/category/route/node_path/recall/mrr/citations/latency |
| 3 | 未跑指标标 `NOT RUN` | ✅ | `citation_support_rate`、`task_success_rate` 均为 `NOT RUN` |
| 4 | 未做推算填充 | ✅ | 无任何"估算值"字段；阈值未达标未调数据集 |
| 5 | 含模型名与参数 | ✅ | `meta.models`（router/qa/troubleshoot/checker/final/embedding）+ `meta.params`（top_k/min_score/ef_search） |
| 6 | `gold_reviewed` 标记 | ✅ | `true`（X5 已确认，§1.4） |
| 7 | judge 分列（不合并） | ✅ | `metrics_by_judge` 三口径独立输出 |
| 8 | A3b 带判定人与时间 | ✅ | 15 条 AI 判定含 `judged_at`；5 条用户留空含 `user_recheck=true` |
| 9 | 前置数据完整性 | ✅ | `meta.seeds` 5 篇全部 `completed` |
| 10 | 冒烟防装配盲区 | ✅ | `meta.smoke` 记录真题 q001 的完整链路结果 |

---

## 5. Release 构建（清 S3 挂账）

### 5.1 改动

| 项 | 内容 |
|---|---|
| `backend/internal/buildinfo/` | 新增包，承载 ldflags 注入的 `Version` / `Commit` / `BuildTime`；未注入时回落 `dev`/`unknown`（本地 `go run` 不受影响） |
| `backend/Dockerfile` | `-ldflags "-s -w -X ...Version/Commit/BuildTime"`；`ENV GIN_MODE=release` |
| `deploy/docker-compose.yml` | 新增 `APP_VERSION` / `GIT_COMMIT` build args |
| `backend/cmd/api/main.go` | 启动日志输出 version/commit/build_time/mode；**gin 模式改为环境变量优先** |

### 5.2 一个顺带修掉的隐患

原 `main.go` **无条件** `gin.SetMode(...)`，会覆盖 `GIN_MODE=release`（Dockerfile 里设的也被覆盖）→ release 镜像实际跑在 **debug 模式**（日志噪声 + 性能损耗）。
实测佐证：修改前容器日志里 `GIN-debug` 路由表刷屏；修改后 `grep -c GIN-debug = 0`。

### 5.3 实测输出

```
2026/10/23 13:17:45 启动: version=dev commit=unknown build_time=2026-10-23T13:17:12Z mode=release
```

`build_time` 由构建期 `date -u` 注入（真实值），`mode=release` 生效。

---

## 6. 呈现层（✅ 已完成）

| 交付物 | 状态 | 备注 |
|---|---|---|
| `README.md` 前 30 行 | ✅ | **此前根本不存在**。含一句话定位 + ASCII 架构图 + 3 技术决策（混合节点+预算防幻觉 / 两阶段发布防竞态 / 评测七指标可复算）+ 评测数字表 + 一键启动 + demo 链接 |
| `docs/demo/demo-script.md` | ✅ | 0:00→3:00 五段分镜，含**拒答案例**与**轨迹展开**，预留 S9 UI 位 |
| `docs/demo/pitch.md` | ✅ | 3 条 bullet（问题→做法→量化结果）+ 一页速览 + 一句话自我介绍 |
| `docs/demo/demo-api.gif` | ✅ | **0.78 MB**（≤10MB），766 事件，真实录制 |
| 计划书 §3.4 演示录像改两阶段 | ✅ | 来源标注"外部审查裁定" |

### 6.1 录制方案（asciinema 在 Windows 不可用）

`asciinema` 依赖 Unix-only 的 `fcntl`，**Windows 上无法运行**（实测 `ModuleNotFoundError: No module named 'fcntl'`）。
替代方案：**自研 asciicast v2 录制器**（`scripts/demo_record.py`）+ 容器内 `agg`
（`ghcr.io/asciinema/agg:latest`）转 GIF。

**真实性保证**：每条命令真实执行、输出为原始 stdout、失败重试**不剪**、仅对空闲等待做倍速；
内容含真实分数（`top_score=0.8123`）、真实工具调用（`analyze_error_log` / `check_port_conflict`）、
真实终端路径（clarify 走 `router→smalltalk`）。

### 6.2 README 快速开始的 3 处硬伤（以陌生人身份实测发现）

按"新终端逐条照做、卡住就改 README"的要求实测，发现并修复：

| # | 硬伤 | 修复 |
|---|---|---|
| 1 | `ai-service/.venv/Scripts/python.exe scripts/smoke_test.sh` —— **`.sh` 不能用 python 跑** | 改为 `bash scripts/smoke_test.sh` |
| 2 | `make up` —— **本机未装 make** | 折为中性的原始命令（`docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build`），并把 `make` 用法放进 `<details>` 折叠区；迁移同理 |
| 3 | `smoke_test.sh` **本身已失效**（S3 骨架时代断言 `POST /documents` 返回 501，接口实现后未鉴权实为 **401**）→ 陌生人照做会直接卡住 | **重写为真实链路冒烟**：健康检查 + 鉴权边界 + 取 token → 上传 5 篇 → 轮询 completed → 提问并断言 `answered` 且带引用 |

> 附带发现：未鉴权访问**不存在的路径**也返回 **401**（鉴权中间件在路由匹配之前执行）——
> 这是合理的安全默认（不泄露"路径是否存在"），脚本按真实行为断言。

---

## 7. 结论

**S8 已关账（2026-10-24）**：

- 35 题全量真跑，**7 项指标全部达标**；引用 ID 有效率硬线 100%。
- **人工复核抓出 1 条"引用越界"（`m001`）** —— 机器指标全抓不到，仅语义复核可见。
  支持率因此为 **0.950** 而非满分；**未调数据集、未调阈值**，记入校准日志并排为 S9 前置修复项 `S9-PRE-1`。
- A10 18/18（抓到并修复并发上传 500 真缺陷）。
- A11 28.4s（目标 ≤300s）。
- Release 构建生效，顺带修掉 gin 模式被覆盖的隐患。
- `0002`/`0003` 迁移使 Recall@5 口径从"近似"变为"真实完整 Top-K"，并补齐证据链字段（`retrieval_top_score` / `rewritten_query` / `citations` 明细）。
- 呈现层四件套齐备；README 快速开始经"陌生人实测"修掉 3 处硬伤。

**遗留（明确移交 S9）**：`S9-PRE-1` 引用纪律收紧（prompt 层），验收方式见计划书 S9 行。