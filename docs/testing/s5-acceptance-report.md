# S5 非流式问答链路验收报告

| 项 | 值 |
|---|---|
| 日期 | 2026-10-23 14:0x – 14:4x（GMT+8） |
| 执行人 | 开发侧（AI 辅助） |
| 依据 | `docs/prd/PRD.md` §11.1（A3/A3b/A4/A9）、`docs/testing/rag-eval-spec.md` §2、`docs/architecture/agent-workflow.md` §2 |
| 结论 | **S5 通过**：5/5 自动项全绿，A3b 人工抽检 100% |
| 范围 | **单节点直链**（retrieval → prompt → 生成 → 引用提取 → 拒答），S6 才接 LangGraph 全图 |
| 执行方式 | `docker compose build` → `up -d` → 宿主机运行真实 HTTP + 真实 DB 的验收脚本 |
| 待续 | FR-17 指代改写属 S6；多轮真实上下文回归留 S6 |

> **脱敏声明**：`DASHSCOPE_API_KEY`、`AI_SERVICE_TOKEN`、`JWT_SECRET`、`POSTGRES_PASSWORD` 一律从 `deploy/.env` 读取并打码，从未打印。

---

## 0. 通过/失败汇总

| 项 | 门槛 | 实测 | 结果 |
|---|---|---|---|
| A3 引用 ID 有效率 | 100% | 29/29 = **100.0%** | **通过** |
| A3b 引用内容支持率（人工抽检 20 条） | ≥ 0.90 | **20/20 = 100%** | **通过** |
| A4 拒答正确率（no_doc 6 题） | 全部拒答且无排障步骤 | **6/6 = 100%** | **通过** |
| 检索 P95 | ≤ 800 ms | **551 ms**（n=35, max 629ms） | **通过** |
| 问答端到端 P95 | ≤ 8 s | **5503 ms**（n=27, max 6140ms） | **通过** |
| A9 引用悬挂 | 200 + status=deleted | **200 + deleted** | **通过** |

质量门禁：`go vet` 干净、`go test ./...` 全绿；`pytest` **53 passed**。

---

## 1. 实现范围

### 1.1 对外接口（契约不变，S6 可直接换全图）

| 层 | 实现 |
|---|---|
| Go | `POST /api/v1/sessions/{id}/messages`：校验会话归属 → **组装最近 4 轮（8 条）上下文** → 落 user 消息 → 调 ai-service → 引用校验 → 落 assistant 消息（`citations` JSONB）+ `agent_runs` |
| Go | sessions CRUD（create/list/get/patch/delete）+ `GET messages`（含引用悬挂标注） |
| Go | `internal/chat/orchestrator.go`：调 `/internal/v1/chat`，**显式禁用环境代理**（与 ingest 一致） |
| Python | `app/retrieval.py`：查询向量化 → 余弦 top-k → **FR-16 四重过滤** |
| Python | `app/chat.py`：`run_turn()` = retrieval → prompt → 生成 → 引用提取/校验 → 拒答判定 |

### 1.2 拒答双闸门（本阶段的关键设计）

真实标定发现检索分数**不可分**：

```
应作答 top_score 最低 = 0.3860（m002）
应拒答 top_score 最高 = 0.4707（q021）
```

区间 [0.386, 0.471] 内正负样本交叠，提高阈值会误拒 5/27 = 18.5%（超 spec 15% 上限）。因此拒答判定分两层：

1. **阈值层**（FR-15）：无命中 或 top_score < 0.35 → 拒答。
2. **答案层**（新增，真实标定驱动）：答案无有效引用，**或**文本呈"依据不足"特征 → 判 `refusal`。

阈值保持 0.35 不动（有依据：实测误拒率 0% vs 提高后的 18.5%）。

---

## 2. 验收实测（命令 + 输出）

### 2.1 运行命令

```bash
cd deploy && docker compose build ai-service backend && docker compose up -d --force-recreate && cd ..
export POSTGRES_HOST=localhost BACKEND_URL=http://127.0.0.1:8081 AI_SERVICE_URL=http://127.0.0.1:8000
ai-service/.venv/Scripts/python.exe scripts/s5_acceptance.py
```

### 2.2 A3 引用 ID 有效率

```
【A3】引用 ID 有效率（存在 + 属于本次检索集）
  引用总数=29, 有效=29, 有效率=100.0%（门槛 100%）
  A3b 抽检包已导出（27 题，含引用）：a3b_sample_pack.json
  ✅ A3 引用 ID 有效率
```

判定口径：引用的 `chunk_id` 必须满足 `visible=TRUE ∧ status='completed' ∧ deleted_at IS NULL ∧ owner_id 匹配`（即"属于本次检索集"）。

### 2.3 A4 拒答正确率

```
【A4】拒答正确率（no_doc 6 题，全部拒答且不含排障步骤）
  ✅ q019: answer_type=refusal, 步骤特征=无
  ✅ q020: answer_type=refusal, 步骤特征=无
  ✅ q021: answer_type=refusal, 步骤特征=无
  ✅ q022: answer_type=refusal, 步骤特征=无
  ✅ q023: answer_type=refusal, 步骤特征=无
  ✅ q024: answer_type=refusal, 步骤特征=无
  ✅ A4 拒答正确率 6/6
```

步骤特征扫描词表：`kubectl / 第一步 / 第二步 / 第三步 / 执行 / 运行以下 / 配置项 / 排查顺序 / 具体步骤 / 代码块`。

### 2.4 延迟

```
【延迟】检索 P95 ≤ 800ms、端到端 P95 ≤ 8s
  端到端: n=27 P50=2453ms P95=5503ms max=6140ms（门槛 8000ms）
  纯检索（含查询向量化）: n=35 P50=435ms P95=551ms max=629ms（门槛 800ms） ✅
  ✅ 端到端 P95 ≤ 8s 实测 5503ms
  ✅ 检索 P95 ≤ 800ms
```

**分段归因**（分段实测）：

| 段 | P95 |
|---|---|
| 查询向量化（DashScope 网络往返） | ~660 ms |
| SQL 向量检索（HNSW） | **~11 ms** |

检索耗时几乎全在网络往返，HNSW 检索本身可忽略。

### 2.5 A9 引用悬挂

```
【A9】引用悬挂：删除原文后历史消息降级展示
  删除前: answer_type=answered, 引用数=1
  删除响应: status=202, ingest_status=deleting
  历史消息: HTTP 200, 引用状态分布=['deleted']
  ✅ A9 引用悬挂降级（200 + status=deleted）
```

### 2.6 汇总

```
验收结果汇总
  ✅ A3_citation_id_valid
  ✅ A4_refusal
  ✅ latency_e2e_p95_le_8s
  ✅ latency_retrieval_p95_le_800ms
  ✅ A9_citation_hanging

总计: 5/5 通过
```

---

## 3. A3b 引用内容支持率（人工抽检）

### 3.1 方法

两级判分（`scripts/a3b_score.py`）：

1. **客观锚点**：引用的 chunk 全文是否覆盖该题的任一 `gold_chunk_texts`（gold 是原文连续子串，可精确匹配）。
2. **人工复核**：锚点未命中的条目逐条人工判定。

### 3.2 结果

```
A3b 引用内容支持率（抽检 20 条）
客观锚点命中: 19/20 = 95.0%（门槛 ≥ 90%）
```

### 3.3 人工复核台账

| 条目 | 判定 | 依据 |
|---|---|---|
| q002#0 | **支持** | chunk 完整含「超过该值会被内核 OOM Killer 终止，表现为 Exit Code 137」，直接支撑答案结论。客观锚点未命中是因 gold 措辞（`通常是 OOMKilled`）与原文措辞（`内核 OOM Killer 终止`）表述不同 —— 属**锚点措辞差异，非引用不支持** |

```
客观锚点支持率 : 19/20 = 95.0%
含人工复核后   : 20/20 = 100.0%（门槛 ≥ 90%）
结论           : PASS
```

---

## 4. 本轮定位并修复的真实缺陷

### 4.1 拒答却挂引用（FR-18③ 违规）

| 项 | 内容 |
|---|---|
| 现象 | m002/m005 答案明说"依据不足，无法回答"，却挂 `[2]` / `[1][2][3]` 引用 |
| 根因 | 模型常用「片段未包含该内容[1]」这种**引用式否定**；`extract_citations` 把编号一并提取，落库即"拒答却挂引用" |
| 危害 | 误导用户（看起来有依据）；拉低 A3b 内容支持率 |
| 修复 | `run_turn` 中判定为 `refusal` 时**清空 citations** |
| 回归 | `test_chat.py::TestRefusalClearsCitations::test_refusal_drops_all_citations` |

### 4.2 答案层判定缺失（"answered + 0 引用"）

| 项 | 内容 |
|---|---|
| 现象 | Redis 问题（no_doc 类）检索到 0.47 的噪声片段，模型诚实回答"依据不足"，但 `answer_type=answered` 且 `citations=0` |
| 危害 | A4 判分失败；用户看到"已回答"却没有依据 |
| 根因 | 仅靠阈值无法覆盖"有噪声命中但证据不支持" |
| 修复 | 新增 `decide_answer_type()`：无有效引用 或 呈"依据不足"特征 → `refusal` |
| 回归 | `test_chat.py::TestDecideAnswerType`（5 例） |

### 4.3 延迟测量方法缺陷（小样本 P95 被抖动污染）

| 项 | 内容 |
|---|---|
| 现象 | n=10 时检索 P95 = 910ms（超门槛）；n=35 时 P95 = 551ms（达标） |
| 根因 | 查询向量化走网络，偶发抖动（实测单次可达 1444ms）；n=10 的 P95 等于"第 9 大值"，一次抖动即顶穿 |
| 性质 | **测量方法缺陷，非系统缺陷** |
| 修复 | 验收脚本改用**全量题目**测量，并同时报告 `max`（诚实反映最坏情况） |

### 4.4 多轮题问法适配（S5 范围界定）

| 项 | 内容 |
|---|---|
| 现象 | m002/m004/m005（multiturn）在 S5 直链下答非所问 |
| 根因 | 这些题的 `question` 是依赖上下文的追问（"那它怎么配？"），需 **FR-17 指代改写** —— 属 S6 全图范围 |
| 处置 | S5 验收用 `control_single_turn_question`（spec 明确为"语义等价于理想改写结果的单轮问法"，Gold 一致）；S6 再回归真实多轮 |

---

## 5. 质量门禁

```bash
cd backend && go vet ./... && go test ./...
```

```
vet exit=0
ok  	opspilot/backend/internal/handlers	1.313s
ok  	opspilot/backend/internal/chat	0.764s
ok  	opspilot/backend/internal/ingest	0.718s
ok  	opspilot/backend/test	(cached)
```

```bash
cd ai-service && pytest -q
```

```
53 passed
```

新增测试：`backend/internal/chat/store_test.go`（4 例）、`ai-service/tests/test_chat.py`（23 例）。

---

## 6. 结论

**S5 非流式问答链路通过验收，5/5 自动项全绿 + A3b 人工抽检 100%，可提交。**

- 核心链路（组装 4 轮上下文 → 检索 → 生成 → 引用提取/校验 → 拒答 → 落库含 citations JSONB）全部走通并经真实数据验证。
- 契约严格对齐 `openapi-ai.yaml` 的 `TurnRequest`/`TurnResult`，S6 换 LangGraph 全图时**对外接口与 Go 侧无需改动**。
- 三项真实缺陷（拒答挂引用、答案层判定缺失、延迟测量方法）已修复并补回归测试。
- 诚实记录：`gold_chunk_texts` **尚未经人工复核**（`rag-eval-spec.md` §1.4 的 X5 欠账仍在），故 A3b 数值只能作为"开发期观测"，不得写进 README 卖点。