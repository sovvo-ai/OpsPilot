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

| 指标 | 值 | 阈值 | |
|---|---|---|---|
| Recall@5 | **1.000** | ≥0.75 | ✅ |
| MRR@5 | **0.812** | ≥0.60 | ✅ |
| 引用 ID 有效率（硬线，独立） | **100%** | 100% | ✅ |
| 多轮保持率（A/B 配对） | **1.000** | ≥0.90 | ✅ |
| 拒答正确率 | **1.000** | ≥0.85 | ✅ |
| 过度拒答率 | **0.000** | ≤0.15 | ✅ |
| 引用内容支持率 | **0.950** | ≥0.90 | ✅ |
| 任务成功率 | **0.950** | ≥0.70 | ✅ |
| 延迟 P95（kb_qa / log_diag） | **7.3s / 12.6s** | ≤8s / ≤25s | ✅ |

> 口径说明：35 题评测集（kb_qa 10 / log_diag 8 / no_doc 6 / ambiguous 3 / citation_risk 3 / multiturn 5），gold 已复核（X5 确认）。
> 报告含 git commit、逐题结果、judge 分列（用户 / AI / 混合三口径），未跑项一律 `NOT RUN`。完整报告见 `ai-service/evals/reports/`。
> 环境：干净环境 `down -v → up -d → 冒烟通过` **28.4s**（目标 ≤5 分钟）。

**⚠️ 人工复核抓到 1 条"引用越界"（`m001`），这条比满分更有说服力。**

人工复核 5 条抽检时发现：`m001`（多轮追问"那它怎么配？"）的答案**把片段之外的通用知识挂在了 KB 引用号下** ——
引用的 `chunk_id` 真实有效（机器判定的"引用 ID 有效率 100%"全过），但**那段内容并不支撑该结论**。

- 这类问题**检索层的 Recall/MRR 抓不到**（检索确实命中了相关片段），**引用 ID 校验也抓不到**（ID 是真的）；
- 只有**人工读语义**才能抓到 —— 这正是把"引用 ID 有效率"与"引用内容支持率"**分成两个指标**的原因：
  合并成一个数会掩盖"引用都存在但都不支持"这种最危险的情况；
- 结果是支持率 **0.950**（19/20）而**不是满分**。我们**没有**为此调数据集或调阈值，
  而是把它记进校准日志并排进修复项（S9 前收紧 final/troubleshoot prompt：只复述片段内的操作建议，
  超出片段的知识不带引用号或显式标注"模型通用知识，无 KB 出处"）。

> 想验证这套评测不是走过场？看 `docs/testing/a3b_user_review.md` 里 5 条的原始判定记录
> 与 `ai-service/evals/reports/*_v2.md` 的校准日志。

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

## 已知限制

- 工具层的端口提取是**启发式**（从问句/附件抽端口），非结构化输入；V1 定位是分析用户提供的文本，不做主机探测。
- `search_knowledge_base` 注册在工具表内，但**按设计不接入 troubleshoot 节点**（计划书 §6.2 声明的可调工具只有 `analyze_error_log` / `check_port_conflict`；主链路 retrieval 已在前，工具内再查 KB 属冗余）。
- 前端（SSE 流式单页）属 S9 范围，当前演示以 API/终端为主。