# OpsPilot · 运维知识库问答与故障排查 Agent

> **一句话定位**：把散落的运维文档（Nginx/MySQL/K8s/网络/可观测性）变成可溯源、会拒答、能带工具排查的问答服务 —— 答案里的每句结论都绑定真实片段，没依据就明说没依据。

**技术栈**：Go（API/摄取/状态机） · Python + LangGraph（Agent 图） · PostgreSQL + pgvector · 通义千问（qwen-plus / qwen-flash）+ text-embedding-v4

```
┌────────────┐   HTTP    ┌──────────────────────────────────────────────┐
│  客户端     │ ────────► │  Go backend :8081                            │
│ (httpx/curl)│           │  · JWT + owner 隔离   · 摄取状态机 + watchdog │
└────────────┘           │  · 会话/消息/轨迹     · 迁移（0001~0003）      │
                         └───────────────┬──────────────────────────────┘
                                         │ 内部接口（X-Internal-Token）
                         ┌───────────────▼──────────────────────────────┐
                         │  ai-service :8000  —— LangGraph 全图          │
                         │                                              │
                         │  router ──► retrieval ──► troubleshoot       │
                         │    │             │            │              │
                         │    │             │        [工具] analyze_     │
                         │    │             │        error_log /        │
                         │    │             │        check_port_conflict│
                         │    │             ▼            ▼              │
                         │    │         evidence ◄───────┘              │
                         │    │             │                           │
                         │    ├──► refuse ◄─┴──► final                 │
                         │    └──► smalltalk                            │
                         └───────────────┬──────────────────────────────┘
                                         │
                         ┌───────────────▼──────────────┐
                         │ PostgreSQL + pgvector        │
                         │ documents / doc_chunks(1024) │
                         │ agent_runs / run_steps /     │
                         │ tool_calls                   │
                         └──────────────────────────────┘
```

## 三个技术决策（面试可讲的点）

**1. 混合节点 + 预算防幻觉 —— "不确定"和"没依据"必须是不同出口。**
图上 5 个业务节点（Router / Retrieval / Troubleshooting / Evidence Checker / Final）+ 2 个确定性终止节点（Refuse / Smalltalk）。Evidence Checker 专门拦"引用存在但不支撑结论"；`max_llm_calls=6` 且 **Final 开局预留 1 次不可抢占**（预算吃紧时 final 仍被真实调用，构造用例断言 `preempted` 不含 final）。拒答与澄清是**代码路径**，不是让模型"尽量别编"。

**2. 两阶段发布防竞态 —— 向量入库与状态推进不在一个事务里。**
摄取分两步：切片先以 `visible=false` 写入，拿到 run_token CAS 成功后才置 `visible=true` 并推进 `ingest_jobs.status`。因此**未完成文档永不参与检索**（有测试）；删除后 Python 迟到的结果 CAS 失败即丢弃，不留残留向量。并发上传同内容由唯一约束 + 23505 捕获兜底（S8 实测修掉的 500 缺陷）。

**3. 评测七指标可复算 —— 报告存"完整 Top-K"，而不是只存被引用的那几条。**
多轮保持率用 A/B 配对法（`ΣRecall_A / ΣRecall_B`），非可比 >40% 直接输出 `NOT RUN` 而不是编个数字。**引用 ID 有效率是 100% 硬线且独立于 7 项**（它测"引用是否存在"，与"引用是否支撑结论"分开 —— 合并会掩盖"引用都存在但都不支持"这种最危险的情况）。报告含 git commit、逐题结果、judge 分列（用户/AI/混合三口径），未跑项一律 `NOT RUN`。

## 评测数字（真实运行，非估算）

> **版本**：**v3-final**（`2026-10-24T1435_v3-final`，git `1334ffc`）。
> 历史版本保留：v2（`_v2.*`）、v3（`2026-10-24T1405.*`）—— **均未覆盖**。

| 指标 | v3-final | 阈值 | |
|---|---|---|---|
| Recall@5 | **1.000** | ≥0.75 | ✅ |
| MRR@5 | **0.812** | ≥0.60 | ✅ |
| 引用内容支持率 | **1.000** | ≥0.90 | ✅ |
| 任务成功率 | **1.000** | ≥0.70 | ✅ |
| 拒答正确率 | **1.000** | ≥0.85 | ✅ |
| 过度拒答率 | **0.000** | ≤0.15 | ✅ |
| 多轮保持率（A/B 配对） | **1.000** | ≥0.90 | ✅ |
| 引用 ID 有效率（硬线，独立） | **100%** | 100% | ✅ |
| 延迟 P95（kb_qa / log_diag） | **6.6s** / 10.0s | ≤8s / ≤25s | ✅ |

> 口径说明：35 题评测集（kb_qa 10 / log_diag 8 / no_doc 6 / ambiguous 3 / citation_risk 3 / multiturn 5），gold 已复核（X5 确认）。
> 报告含 git commit、逐题结果、judge 分列（用户 / AI / 混合三口径），**未跑项一律 `NOT RUN`**，不填推算值。
> 环境：干净环境 `down -v → up -d → 冒烟通过` **28.4s**（目标 ≤5 分钟）。

**每个数字的出处（逐项可查，不靠印象）**

| 数字 | 出处 |
|---|---|
| Recall@5 / MRR@5 / 拒答正确率 / 过度拒答率 / 多轮保持率 | [`2026-10-24T1435.md`](ai-service/evals/reports/2026-10-24T1435.md) §1 指标表 |
| 引用内容支持率 / 任务成功率 1.000（三口径） | [`2026-10-24T1435_v3-final.md`](ai-service/evals/reports/2026-10-24T1435_v3-final.md) §2；判定记录 [`a3b_user_review_v3.md`](docs/testing/a3b_user_review_v3.md) |
| 延迟 P95 6.6s / 10.0s；方案 A 前后对比 | [`s8v3-eval-report.md`](docs/testing/s8v3-eval-report.md) §1「延迟」；复测脚本 `scripts/kb_qa_latency.py` |
| 引用 ID 有效率 100% | 同上报告「引用 ID 有效率（硬线，独立于 7 项）」 |
| 残留（检测器方差 / V2 候选） | [`v3_residual_notes.md`](docs/testing/v3_residual_notes.md) |
| 异常矩阵 18/18 | [`s8-acceptance-report.md`](docs/testing/s8-acceptance-report.md) §2 |
| 冷启动 28.4s | 同上 §3（A11 分阶段计时） |

**⭐ 支持率 1.000 的读法（请务必看这段）**

这是 **5 条人工抽检 + 15 条 AI 判定** 的**混合口径**，**不是"每条答案每句都干净"**：

- **残留（未根治）**：`citation_overreach` 检测器在 5 次重跑采样中**仍有命中** ——
  模型会把**与问题无关的文档**（如《Linux 网络故障排查》）的编号挂到探针结论上。
  本抽检的 `m001` 抽到的是**干净版本**，方差真实存在。
- **本报告不做"引用越界已根治"表述**。详情见
  [`docs/testing/v3_residual_notes.md`](docs/testing/v3_residual_notes.md)。
- **为什么仍值得展示**：v2 时这类问题**机器完全抓不到**（引用 ID 有效、Recall 正常），
  只有人工读语义才发现。现在我们有了 `citation_overreach` 检测器把**它变成可观测、可回归**，
  并明确列出两条 V2 候选改进（检索主题过滤、rewrite 后二次收敛）。
  这比"报个满分"更能说明评测体系是**真在跑**的。

## 快速开始

> **前置**：Docker Desktop 已启动；Python **3.11**（用于建 ai-service 的 venv）。
> `make` **可选** —— 本机没装 make 就直接用下面的原始命令（等价）。

### 1. 配置

```bash
git clone <repo> && cd OpsPilot
cp deploy/.env.example deploy/.env
# 编辑 deploy/.env，至少填 DASHSCOPE_API_KEY（百炼）；其余可保持默认
```

### 2. 起服务栈（backend + ai-service + postgres）

```bash
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
```

<details><summary>装了 make 的话，等价于 <code>make up</code></summary>

```bash
make up        # = 上面那条 docker compose 命令
```
</details>

### 3. 建 Python 环境并跑迁移

```bash
# 用 Python 3.11 建 venv（路径按你的实际安装调整）
"C:/Users/<你>/AppData/Local/Programs/Python/Python311/python.exe" -m venv ai-service/.venv

# 装依赖
ai-service/.venv/Scripts/python.exe -m pip install -r ai-service/requirements.txt

# 执行数据库迁移（0001~0003）
ai-service/.venv/Scripts/python.exe scripts/run_migrate.py up
```

<details><summary>装了 make 的话，第 3 步的迁移等价于 <code>make migrate</code></summary>

```bash
make migrate
```
</details>

> **Linux/macOS**：把 `ai-service/.venv/Scripts/python.exe` 换成 `ai-service/.venv/bin/python`。

### 4. 冒烟（确认全链路通）

```bash
# 健康检查 + 鉴权边界 + 真实链路（上传 5 篇 → 摄取 → 提问）
# 注意：这是 **bash 脚本**，要用 bash 跑，不能用 python 跑
bash scripts/smoke_test.sh
```

预期输出结尾：

```
[未鉴权 documents -> 401] 401
[不存在的路径（未鉴权）-> 401] 401
[端到端链路]
  token ok
  已上传 5 篇
  5 篇摄取 completed
  提问 -> answer_type=answered, citations=2
  端到端链路 ok
== 冒烟测试通过 ==
```

### 5. 跑评测（可选，约 4 分钟）

```bash
cd ai-service
.venv/Scripts/python.exe -m evals.run_eval
# → evals/reports/<ts>.json + .md（含逐题结果与 judge 分列三口径）
```

> **Windows 用户注意**：所有含中文的 HTTP 调用请用 Python `httpx`，
> **不要用 Windows shell `curl`** —— 中文参数会被转义破坏（见 `test-plan` G7）。

## 演示

- **浏览器版演示（推荐）**：[`docs/demo/demo.gif`](docs/demo/demo.gif) —— 真前端录屏（2.6MB），
  覆盖上传 → 提问带引用 → 点角标看片段 → 拒答（列出最高分片段）→ 澄清（不猜）→ 轨迹展开
- 终端 / API 版演示：[`docs/demo/demo-api.gif`](docs/demo/demo-api.gif) —— 同一套后端，
  命令行走完整链路（含端口冲突工具调用）
- 3 分钟分镜脚本：[`docs/demo/demo-script.md`](docs/demo/demo-script.md)
- 求职 pitch：[`docs/demo/pitch.md`](docs/demo/pitch.md)

> 两个 GIF 均为**真实录制**（`scripts/s9_demo_record.py` / `scripts/demo_record.py`），
> 仅对空闲等待做倍速压缩，未剪辑失败重试。页面文本证据见
> [`docs/testing/s9-ui-smoke.md`](docs/testing/s9-ui-smoke.md)（38/38 冒烟）。

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/prd/PRD.md`](docs/prd/PRD.md) | 需求、接口、验收标准（A1~A15） |
| [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md) | 总体架构与选型 |
| [`docs/architecture/agent-workflow.md`](docs/architecture/agent-workflow.md) | LangGraph 图、预算、降级 |
| [`docs/architecture/tool-policy.md`](docs/architecture/tool-policy.md) | 工具白名单、注入防护 |
| [`docs/testing/rag-eval-spec.md`](docs/testing/rag-eval-spec.md) | 评测口径与报告 schema |
| [`docs/testing/test-plan.md`](docs/testing/test-plan.md) | 分层测试与全局约定（G1~G7） |
| `docs/testing/s{4,5,6,7,8}-acceptance-report.md` | 各阶段验收报告（含实测输出） |
| [`docs/testing/s9-ui-smoke.md`](docs/testing/s9-ui-smoke.md) | S9 前端冒烟（U1–U10 + UI-10 + auth 豁免反向用例，38/38） |
| [`docs/testing/s8v3-eval-report.md`](docs/testing/s8v3-eval-report.md) | v3 评测报告（含未达标项与残留分析） |
| [`docs/testing/v3_residual_notes.md`](docs/testing/v3_residual_notes.md) | 残留与已知限制（检测器方差、V2 候选） |

## 测试口径（两种口径，都贴命令原文）

测试数**不写单一数字**，因为依赖是否连着栈（11 个集成用例需要数据库）：

```bash
# 口径 A：无栈基线（不传 POSTGRES_*，集成用例自动跳过）
cd ai-service && .venv/Scripts/python.exe -m pytest -q
# → 210 passed, 11 skipped

# 口径 B：全栈环境（栈已起 + POSTGRES_* 指向它，集成用例真实执行）
cd ai-service
POSTGRES_HOST=localhost POSTGRES_PORT=55432 POSTGRES_USER=ops_pilot \
POSTGRES_PASSWORD=<见 deploy/.env> POSTGRES_DB=ops_pilot \
.venv/Scripts/python.exe -m pytest -q
# → 221 passed
```

Go 侧：

```bash
cd backend && go vet ./... && go test ./...
# → 四个包全 ok：chat / handlers / ingest / test
```

> **纪律**：以上数字均来自命令原始输出，**不做"通过 + 跳过 = 总数"的加法换算**。

## 已知限制

- **kb_qa 长答案延迟贴近阈值**（v3-final：P95 = 7.8s / 阈值 8s）。方案 A（简洁度纪律 +
  `max_tokens` 512）把该题从 10.1s 降到 7.8s，但**余量有限**；上游模型对长答案的生成
  时延仍会使其在恶劣网络下波动超阈。**未调阈值**，如实记录。
- **引用越界未根治**：`citation_overreach` 检测器在 5 次重跑采样中仍有命中（模型把
  **与问题无关的文档**的编号挂到结论上）。支持率 5/5 只覆盖「被抽中的结论句」，
  **不等于每条答案每句都干净**。根因是检索集混入无关文档 → V2 候选见
  [`docs/testing/v3_residual_notes.md`](docs/testing/v3_residual_notes.md)。
- 工具层的端口提取是**启发式**（从问句/附件抽端口），非结构化输入；V1 定位是分析用户提供的文本，不做主机探测。
- `search_knowledge_base` 注册在工具表内，但**按设计不接入 troubleshoot 节点**（计划书 §6.2 声明的可调工具只有 `analyze_error_log` / `check_port_conflict`；主链路 retrieval 已在前，工具内再查 KB 属冗余）。