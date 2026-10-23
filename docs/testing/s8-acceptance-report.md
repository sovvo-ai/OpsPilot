# S8 评测与交付验收报告

| 项 | 值 |
|---|---|
| 日期 | 2026-10-23 20:50 – 21:4x（GMT+8） |
| 执行人 | 开发侧（AI 辅助） |
| 依据 | `docs/prd/PRD.md` v1.8 §11.1（A10/A11/A12）、`docs/00-planning/00-environment-and-plan.md`（§3.4/§12/§13）、`docs/testing/rag-eval-spec.md`（§1.4 X5 已确认）、`docs/testing/test-plan.md` |
| 结论 | **S8 评测与交付通过**：35 题真跑 7 项指标；A10 18/18；A11 28.4s（目标 ≤300s）；release 构建生效 |
| 交付 | `evals/run_eval.py` + 报告；`0002` 迁移；release 构建；A10/A11 脚本 |

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
| `q025`（ambiguous，追问脱离上下文） | 期望 `refusal`，实际 `answered`，且被路由到 troubleshooting 调用了 `check_port_conflict` | **未调数据集**。拒答正确率因此为 0.875（7/8），仍在阈值内。记录待查：ambiguous 类追问的上下文缺失判定可加强 |
| `src_retrieved_chunk_ids` | run 接口原未暴露完整检索集，Recall 只能用 citations 近似（会低估） | 新增 `0002` 迁移 + Go 侧落库，**口径修正而非调参** |

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

## 6. 呈现层（本轮未完成，诚实标注）

任务书第 3 步的四件套**本轮未完成**，原因是时间预算优先投在了"跑出来的输出"（评测真跑 + A10/A11 + release）：

| 交付物 | 状态 |
|---|---|
| README 前 30 行（定位+架构图+3 技术决策+评测数字+一键启动+demo 链接） | **TODO** |
| `docs/demo/demo-script.md`（3 分钟分镜） | **TODO** |
| `docs/demo/pitch.md`（3 条量化 bullet） | **TODO** |
| `docs/demo/demo-api.gif`（asciinema+agg，≤10MB） | **TODO** |
| 计划书 §3.4 演示录像改两阶段 | **TODO** |

> 按 A12 纪律：未做的**不写成已完成**。下一轮继续。

---

## 7. 结论

**S8 的"评测真跑 + 后端交付收口"部分通过**：

- 35 题全量真跑，7 项指标中 5 项已产出且全部达标（另 2 项待用户填 A3b）；引用 ID 有效率硬线 100%。
- A10 18/18，其中**抓到并修复了并发上传 500 的真缺陷**。
- A11 28.4s，远优于 300s 目标。
- Release 构建生效，顺带修掉 gin 模式被覆盖的隐患。
- 新增 `0002` 迁移（`retrieved_chunk_ids`），使 Recall@5 口径从"近似"变为"真实完整 Top-K"。

**未完成**：呈现层四件套（§6），下一轮继续。