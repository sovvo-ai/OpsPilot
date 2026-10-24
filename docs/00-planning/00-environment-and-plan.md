# OpsPilot 阶段 0：环境检查、范围冻结与前置验证

> 版本：v4.4（2026-10-22 19:43 修 F7 超时值漏改、F10 双写例外、F11 去重码、G3 `attempt` 语义；补 SPIKE partial 索引用例与文档维护规则）
> 状态：**已冻结（2026-10-22 评审通过）。仅允许 S1 SPIKE 实测结果触发修改；纯措辞/结构性打磨一律拒绝**
> 本阶段不产出任何业务代码。
>
> **安全声明**：本文档、代码、日志、测试输出与 Git 提交中均不得出现真实 API Key。Key 只允许存在于本地 `deploy/.env`（已 gitignore）。会话中曾暴露的那枚 Key **已于 2026-10-22 由用户轮换完成**（用户确认），本文档不记录其内容，也不再保留任何历史值。

---

## 1. 环境检查报告（保留 v1 实测结果）

### 1.1 工作区状态

| 检查项 | 实测结果 | 结论 |
|---|---|---|
| 工作目录 | `<项目根目录>`（本项目） | 存在 |
| 目录内容 | v1 检查时为空；当前仅 `docs/`、`.workbuddy/` | 无用户已有文件，可安全初始化 |
| Git 仓库 | `fatal: not a git repository` | 未初始化 |
| Git 版本 | 2.55.0.windows.3 | 可用 |
| Git 身份 | 已配置（具体署名见提交历史） | 已配置 |

### 1.2 操作系统

| 项 | 值 |
|---|---|
| OS | Windows 10.0.26200（Windows 11 系）x86_64 |
| Shell | Git Bash（POSIX）+ PowerShell 5.1 |
| 编码风险 | 工作区路径含中文（非 ASCII）→ 全链路强制 UTF-8，文件名需规范化 |

### 1.3 语言运行时

| 运行时 | 版本 | 路径 | 结论 |
|---|---|---|---|
| Go | 1.27.1 windows/amd64 | 系统 PATH | 可用 |
| GOPROXY | `https://goproxy.cn,direct` | 已配国内源 | 实测 200 / 1.5s |
| GOSUMDB | `sum.golang.org` | 默认 | 直连不通，首次 `go mod tidy` 需实测处理 |
| Python（主） | 3.13.14 | conda base（路径因机器而异） | 可用，**禁止**直接 pip 安装污染 base |
| Python（备） | 3.11.9 | 系统 Python 3.11（路径因机器而异，如 Windows 下 `<用户目录>/AppData/Local/Programs/Python/Python311/python.exe`）（**S1 实测**：原记录的 managed 路径不存在，已更正为系统 Python 3.11） | 与容器 `python:3.11-slim` 对齐，**用它建 venv** |
| pip | 26.0.1 | conda base | 未配置镜像源；PyPI 实测 200 但 10.5s，建议 venv 内配国内源 |
| Node | v22.22.2 / 24.14.1 | managed / 系统安装（路径因机器而异） | V1 不需要；V1.1 前端用原生 JS，也不需要 |

已装相关包：`pydantic 2.13.2`、`pydantic-settings 2.12.0`、`uvicorn 0.51.0`、`httpx 0.28.1`、`numpy 2.5.1`。
未装：fastapi、langgraph、sqlalchemy、psycopg、pgvector、pytest、openai。

### 1.4 容器环境

| 项 | 实测结果 |
|---|---|
| Docker | 28.5.1（Docker Desktop），daemon 运行中 |
| Compose | v2.40.3-desktop.1 |
| 本地镜像 | `pgvector/pgvector:pg16`、`postgres:16(+alpine)`、`python:3.11-slim`、`golang:1.27.2-alpine`、`alpine:3.20`、`redis:7*`、`nginx:alpine` 等 |
| 运行中容器 | `pg-local`(5432)、`redis-local`(6379)、`blog-meili`(7700)、`blog-mysql`(3307)、`blog-redis`(6380) |

**结论：`pgvector/pgvector:pg16`、`python:3.11-slim`、`golang:1.27.2-alpine`、`alpine:3.20` 均已在本地，不拉镜像即可完成部署目标。**

### 1.5 网络连通性

| 目标 | 结果 | 判定 |
|---|---|---|
| `dashscope.aliyuncs.com/compatible-mode/v1` | 401（无 key）/ 200（带 key） | **可达** |
| `api.deepseek.com` | 401 / 200 | 可达但本项目不使用 |
| `registry-1.docker.io` | 超时 000 / 10s | **不可直连** |
| `docker.m.daocloud.io` | 401 认证挑战 / 1.8s | 可达（备用 registry-mirror） |
| `proxy.golang.org` | 超时 000 | 不可用，已由 goproxy.cn 替代 |
| `pypi.org/simple` | 200 / 10.5s | 可达但慢 |
| `huggingface.co` / `hf-mirror.com` | 均超时 000 | 不可达（本项目已不依赖） |
| `modelscope.cn` | 302 / 0.7s | 可达（已不依赖） |

**容器出网（S4 实测补充，来源=实测）**：

| 项 | 观测 | 处置 |
|---|---|---|
| Docker 容器出网方式 | 本机容器**可直连外网**（`dashscope.aliyuncs.com` 直连可达）；走宿主代理 `host.docker.internal:7890` 反而 `Connection refused` | 容器内代理留空，外呼客户端 `trust_env=False` |
| 代理变量继承 | Docker Desktop 会把宿主 `HTTP(S)_PROXY` **（含小写 `http_proxy`/`https_proxy`）** 注入容器；compose 只清大写变量时小写仍泄漏，容器内 `127.0.0.1` 指向容器自身 → 出网失败 | 外呼显式 `trust_env=False`；仅当配置 `OUTBOUND_PROXY` 时才走代理（与 Go 侧 `Transport{Proxy: nil}` 对齐） |

> 这是 **S4 摄取首轮近半用例失败的真实原因**（Embedding 全部 `EMBEDDING_FAILED / Connection refused`）：容器继承了宿主代理指向，而宿主代理客户端未对容器可达。属**环境前置条件**，非业务缺陷。

### 1.6 端口占用（必须避让）

| 端口 | 状态 | 占用者 | OpsPilot 处置 |
|---|---|---|---|
| 5432 | 已占用 | `pg-local` | 宿主改用 **55432** |
| 6379 | 已占用 | `redis-local` 等 | **不使用 Redis** |
| 8080 | 已占用 | PID 43932 | Go API 用 **8081** |
| 8000 | 空闲 | — | AI 服务用 8000 |
| 3000 / 5173 | 空闲 | — | 预留给 V1.1 前端 |

### 1.7 磁盘

| 盘 | 可用 | 使用率 |
|---|---|---|
| E:（项目 + Docker 数据） | **31G** | 97% |
| C: | **18G** | 92% |

约束：禁止 torch / CUDA / 大模型镜像；控制镜像层数与构建缓存。

### 1.8 环境风险

| # | 风险 | 处置 |
|---|---|---|
| E1 | Docker Hub 不可直连 | 只用本地已有镜像；必要时配 daocloud mirror |
| E2 | `GOSUMDB` 直连不通 | 阶段 1 首次 `go mod tidy` 实测，必要时本地调整 |
| E3 | 中文路径 + UTF-8 | 全链路 UTF-8；文件名 NFC 规范化 |
| E4 | 磁盘仅剩 31G | 镜像瘦身；`docker builder prune` 写入 Makefile |
| E5 | 端口冲突 | 55432 / 8081 / 不用 Redis |
| E6 | Bash PATH 缺少 `make`；清华镜像出现假冒高版本 Python 包 | make 使用绝对路径 `/c/Program Files (x86)/GnuWin32/bin/make.exe`；ai-service 依赖锁定官方 PyPI 并离线 vendor 化 |
| E7 | Docker 容器继承宿主代理变量（含小写），容器内 `127.0.0.1` 指向自身 → 出网失败（S4 实测） | 外呼客户端 `trust_env=False`；容器内代理留空。详见 §1.5 容器出网表 |

---

## 2. 关键决策与待验证事项

### 2.1 已确认（Decided）

| # | 决策 | 依据 |
|---|---|---|
| D1 | LLM 与 Embedding **全部使用阿里云百炼**，单一 base_url + 单一 Key | 用户指令（2026-10-22） |
| D2 | 统一走 OpenAI 兼容入口 `https://dashscope.aliyuncs.com/compatible-mode/v1` | 实测可用，Go/Python 复用同一调用形态 |
| D3 | Embedding 用 `text-embedding-v4`，**1024 维** | 实测默认返回 1024 维 |
| D4 | 保留 LangGraph，但**节点不强制调模型**，确定性工作交代码 | 见 §6 |
| D5 | 不使用 Redis / 消息队列 / 分布式事务 | 范围与复杂度约束 |
| D6 | 单表单写者：Go 与 Python 各自只写自己的表。**例外**：`doc_chunks` 为受约束双写——Python 写内容（`visible=false`）、Go 只改 `visible` 与按 token 清理 | 见 §7.5；例外见 `architecture/ingest-consistency.md` |
| D7 | V1 只做后端与 API 契约；前端放 V1.1 | 见 §3 |
| D8 | 工具限 3 个纯函数，禁止任意 Shell/SQL/文件路径 | 见 §8 |
| D9 | 评测统一 **7 项**指标；未执行的标 `NOT RUN` | 见 §9 |
| D10 | **原文存 PostgreSQL 独立表 `document_contents`**，不用共享 volume | 事务一致、删除级联、重试必能读到原文、备份统一；见 §7.6 |
| D11 | 多轮指代**做条件式 query 改写**（Retrieval 内子步骤，非新节点），命中指代词且非首轮才调 1 次 `qwen-flash` | 不加节点即可用，主路径仍 ≤3 次生成调用；见 §6.2 |
| D12 | Go ↔ Python 用 **shared secret header `X-Internal-Token`**，常量时间比较 | 见 §10.1 |
| D13 | 引用以 **JSONB 快照**存进 `messages`，删除后降级为"原文已删除"占位 | 见 §7.7 |
| D14 | `max_llm_calls=6`（由 troubleshooting 最坏路径推导）+ **Final 开局预留 1 次不可抢占** + 四档抢占优先级 | 见 §6.3；v3 的 4 只覆盖 kb_qa，是算术错误 |
| D15 | 评测 30 → **35 题**，新增 `multiturn` 类 5 条，每条配同义单轮对照 + 新增"多轮保持率"指标 | 见 §9；v3 只有承诺没有承载字段 |
| D16 | **Go 独占摄取任务状态**；Python 只产数据（`doc_chunks.visible=false`），Go 用 `run_token` CAS 发布 | 消除双写终态；见 §7.3 |
| D17 | 状态机扩为五态（加 `deleting`），删除走"阻断→清向量→删行" | 防"记录已删、向量残留"；见 §7.4 |
| D18 | **V1 不自动重试**，`attempt` 仅为执行世代；`pending`/`processing` 均由 Go watchdog 收敛 | 无队列的自动重试会重复烧 Embedding 费用 |

### 2.2 已验证（VERIFIED，2026-10-22 18:00–18:10 真实请求）

| 项 | 实测结果 |
|---|---|
| 鉴权 | 有效 Key → 200；错误 Key → **HTTP 401** `code=invalid_api_key` |
| 模型不存在 | **HTTP 404** `code=model_not_found`，message 明确 |
| 可用模型数 | `/models` 返回 **178** 个，含 qwen / deepseek / kimi / glm / MiniMax 等 → **该端点实为模型网关，非纯百炼目录** |
| 聊天最小请求 | `qwen-flash` **0.45s**（15 tok）；`qwen-plus` **0.67s**；`qwen3.8-max` **2.64s**（含 16 reasoning tok） |
| JSON 模式 | `response_format={"type":"json_object"}` 生效 |
| 流式 | `stream=true` 正常返回 SSE chunk |
| Embedding 最小请求 | `text-embedding-v4` 成功，**dim=1024**，usage 正常 |
| `dimensions` 参数 | 传 1024 生效；`qwen3.7-text-embedding` 同为 1024 维 |
| Embedding 批量上限 | **单请求最多 10 条**；11 条 → HTTP 400 `InvalidParameter: batch size is invalid, it should not be larger than 10` |
| `qwen-turbo` 裸名 | **列表中不存在**，仅有 `qwen-turbo-0919` / `qwen-turbo-2024-11-01` → v1 的模型档位需修正 |

### 2.3 待验证（NOT VERIFIED —— 阶段 1 必须跑通，未过不得进入主体实现）

| 项 | 卡点 | 验证方式 |
|---|---|---|
| **pgvector 写入/检索** | 只确认镜像存在，未在任何库执行过 `CREATE EXTENSION vector` 与向量查询 | 独立容器（宿主 55433）→ 建 `vector(1024)` 表 → 插 3 条 → 余弦 top-k → 断言顺序 |
| **HNSW 索引** | pgvector 版本与构建参数未知 | `CREATE INDEX ... USING hnsw (embedding vector_cosine_ops)` 成功即通过 |
| **429 限流真实形态** | 未触发过，不知 header 与文案 | 并发压测 embed（20 并发）观察首个 429 响应结构 |
| **超时行为** | 未测长 prompt / 慢响应 | 大 max_tokens 请求，测客户端 timeout 与断连表现 |
| **Go ↔ Python 契约** | 两侧都还没写 | 最小 echo 服务验证 JSON、中文、超时、错误透传 |
| **单请求输入上限** | 未测最大输入 tokens | 逐步加大输入找边界，用于确定切片上限 |
| **网关模型稳定性** | 列表含大量第三方模型，可能随网关策略变动 | 启动探测目标模型，缺失则降级映射并上报 `DEGRADED` |

> 强制规则：**任何"应该可以"不得直接变成代码假设**。可执行清单见 §12。

---

## 3. V1 范围冻结

### 3.1 V1 必须交付（后端闭环，缺一不可）

| # | 能力 | 验收锚点 |
|---|---|---|
| 1 | Markdown/TXT 上传（≤5MB），UTF-8 规范化 | 上传 → 摄取 → 可检索，全链路状态可见 |
| 2 | 解析、切片、Embedding（1024d）、向量入库 | 幂等、可重试、失败可见 |
| 3 | RAG 问答 + 真实引用（chunk 级，可回溯原文） | 引用 ID 必须存在且属于本次检索集 |
| 4 | 无充分证据时拒答 | 无相关文档子集拒答正确 |
| 5 | LangGraph 工作流：**5 个业务节点 + 2 个确定性终止节点**（口径统一，见 §6.2） | 每节点独立 trace；步数与预算受限 |
| 6 | 3 个白名单工具 | 非白名单一律拒绝；注入用例全拦 |
| 7 | 会话、消息持久化、轨迹查询 | 轨迹能还原完整执行链 |
| 8 | RAG 评测（**35 题**，含 5 条多轮 + 5 条对照） | 六项基础指标 + 多轮保持率真实产出；未跑的标 NOT RUN |
| 9 | Docker Compose 一键启动 | 干净环境到冒烟 ≤5 分钟 |
| 10 | OpenAPI 契约文档 | 前后端可据此并行 |
| 11 | **多轮追问可用**：指代式追问的 Recall@5 不低于同义单轮对照的 90% | §9.5 A/B 配对（5 条多轮 + 5 条对照）+ 多轮保持率 ≥ 0.90 |
| 12 | **历史引用在原文删除后可解释** | 返回 `status=deleted` + 快照片段，不 500、不空白 |

### 3.2 V1.1 交付（后端闭环之后）

- **轻量 Web 页面**：单页，原生 HTML/CSS/JS，**不引入 React/Vue 脚手架、不引入构建链**。
- 功能下限：聊天（含流式）、文档上传与状态展示、引用查看（点开看原文片段）、历史会话、Agent 执行轨迹查看。
- 后端为支持前端补充：SSE 流式接口、静态资源托管（Go `embed` 打包或 Nginx 二选一）。

### 3.3 明确不做（V1 / V1.1 均不含）

| 不做 | 原因 |
|---|---|
| 真实主机诊断（`ss`/`netstat`/读日志文件） | 安全边界；V1 只处理用户粘贴文本与模拟数据 |
| 任意 Shell / 任意 SQL / 任意文件路径读取 | 不开这个口子 |
| PDF/Word/HTML 解析、OCR、图片 | 成本高，非核心证明点 |
| 多租户、RBAC、细粒度权限 | 单用户演示足够 |
| Kubernetes / Kafka / 微服务治理 | 明确排除 |
| Redis / 消息队列 / 分布式锁 / 分布式事务 | 明确排除 |
| 前端复杂框架、组件库、设计系统 | 与"轻量演示"冲突 |
| rerank 模型、混合检索调优 | 列 P2 可选；V1 只用向量检索 + 阈值 |

### 3.4 呈现层交付物（求职入口，与代码同等重要）

> 招人的人不会读 14 节计划书。能不能被看见，取决于下面四样东西。**它们是 S8 的验收项，不是可选项。**

| 交付物 | 位置 | 硬要求 |
|---|---|---|
| README 亮点区 | `README.md` 顶部 30 行内 | 一句话定位 + 架构图 + **3 个具体技术决策**（混合节点省了什么、引用校验怎么防幻觉、幂等怎么做）+ 评测数字（真实值，未跑写 NOT RUN）+ 一键启动命令 |
| 3 分钟 Demo 脚本 | `docs/demo/demo-script.md` | 分镜写清每步做什么、说什么、预期画面；**必须包含一条拒答案例**（问一个库里没有的问题，展示拒答而非编造）+ 一次轨迹展开（展示 5 业务节点跑了什么、实际调了几次模型） |
| 演示录像 | **两阶段**（来源 = 外部审查裁定，2026-10-23 S8）：<br>① **S8**：`docs/demo/demo-api.gif`（≤10MB）—— **API/终端**真实录制，覆盖上传→提问→引用→拒答→澄清→轨迹展开；<br>② **S9**（前端就位后）：`docs/demo/demo.gif`（≤10MB）—— **浏览器**真实录制，覆盖同一分镜的 UI 形态 | 真实录制，不剪辑伪造（禁摆拍/预置结果；失败重试不剪，仅对空闲等待倍速压缩）。分阶段理由：S8 阶段前端尚未交付（S9 范围），UI 录像无从录制，强录会造假 |
| 简历/面试用要点 | `docs/demo/pitch.md` | 3 条 bullet，每条"问题→做法→可量化结果"，不写"使用了 XX 技术"式罗列 |

验收口径：陌生人只看 README + GIF，能在 3 分钟内说出这个项目解决什么问题、技术上哪里不平凡。做不到就是没交付。

---

## 4. 推荐项目目录结构（V1 调整版）

```
OpsPilot/
├── README.md  CHANGELOG.md  .gitignore  .editorconfig  Makefile
│
├── docs/
│   ├── 00-planning/            # 本文件
│   ├── prd/                    # PRD、用户故事、范围冻结
│   ├── architecture/           # 架构、数据模型、ADR、graph 设计、工具策略
│   ├── api/                    # 两份 OpenAPI、错误码、示例请求
│   ├── testing/                # 测试计划、矩阵、评测规范、验收清单
│   └── ops/                    # 部署、环境变量、runbook
│
├── backend/                    # Go + Gin（对客户端唯一入口）
│   ├── cmd/api/
│   ├── internal/{config,http/{middleware,handler,dto},service,repo,aiclient,domain,obs}
│   ├── migrations/             # 仅 Go 拥有的表
│   ├── test/                   # 集成测试（真实 PG）
│   └── go.mod
│
├── ai-service/                 # Python 3.11 + FastAPI
│   ├── app/
│   │   ├── api/                # ingest / retrieve / chat / tools / health
│   │   ├── core/               # settings, logging, errors, security
│   │   ├── providers/bailian/  # llm.py / embeddings.py（唯一出网点）
│   │   ├── ingest/             # loader → chunker → embedder → writer
│   │   ├── rag/                # retriever / prompt / citation
│   │   ├── graph/              # state.py, nodes/, builder.py, budget.py
│   │   ├── tools/              # registry + 3 个纯函数工具
│   │   └── db/
│   ├── tests/{unit,integration}
│   ├── evals/{datasets,reports,run_eval.py}
│   └── pyproject.toml  Dockerfile
│
├── deploy/
│   ├── docker-compose.yml      # postgres(pgvector) + ai-service + backend
│   ├── docker-compose.dev.yml  # 仅 PG
│   ├── .env.example            # 不含真实 Key
│   └── initdb/                 # CREATE EXTENSION vector
│
├── scripts/{dev_up,smoke_test,seed_docs}
│
└── web/                        # ❌ 已取消（来源=外部审查裁定，2026-10-24）
```

> **裁定（来源=外部审查裁定，2026-10-24）**：前端静态资源**不落根级 `web/`**，改为随宿主
> 服务走 —— 位于 `backend/internal/web/static/`，由 Go `//go:embed` 打包托管。
> 理由：embed 资源跟着宿主服务走（版本天然对齐、仓库根少一个孤儿目录、少一个组件），
> 与 PRD §8.1「不引入第三套服务」一致。**文档与现实不许各说各话**：本行原列
> `web/` 已作废，实际位置以本裁定为准。

---

## 5. 文档清单

| 优先级 | 文档 | 验收标准 |
|---|---|---|
| P0 | `docs/prd/PRD.md`（**用户故事与范围已并入正文 §5/§4，不再单独产出 `user-stories.md` / `scope.md`**） | 每条 P0 功能有可测验收条件；非目标清单冻结 |
| P0 | `docs/architecture/data-model.md` | DDL 可执行；写者矩阵与 §7.4 一致；含 `document_contents`（原文表）与 `messages.citations` 快照结构 |
| P0 | `docs/api/openapi-backend.yaml` / `openapi-ai.yaml` | lint 通过；无 `TBD`；错误码齐全 |
| P0 | `docs/testing/rag-eval-spec.md` | 7 项指标公式（含多轮保持率）+ 数据集格式 + 35 题分布 + A/B 对照方法 |
| P1 | `docs/architecture/agent-workflow.md` | 5 业务节点 + 2 终止节点的 IO 契约 + 模型调用条件 + 预算 |
| P1 | `docs/architecture/tool-policy.md` | 3 工具入参/出参/限制 + 注入防护用例 |
| P1 | `docs/architecture/ARCHITECTURE.md` | 上传/问答两条链路时序图 |
| P1 | `docs/testing/test-plan.md` + `test-matrix.md` | 功能点 × 测试类型矩阵 |
| P1 | `docs/testing/spike-report.md` | 阶段 1 实测记录（命令/预期/实际） |
| P2 | `docs/ops/{deployment,env,runbook}.md` | 环境变量全表；常见故障处置 |
| P2 | `docs/architecture/adr/000N-*.md` | 关键取舍有记录 |
| P2 | `docs/demo/demo-script.md` + `pitch.md` + `demo.gif` | 3 分钟分镜可复现；含拒答案例与轨迹展开；pitch 三条 bullet 有量化结果 |

---

## 6. LangGraph 工作流：收敛设计

### 6.1 设计原则

1. **能用代码判定的，不调模型。** 检索、过滤、ID 校验、格式化、阈值判断全部用确定性代码。
2. **模型调用有硬预算。** 每轮最多 **6** 次生成调用（由 troubleshooting 最坏路径推导，见 §6.3）；默认 kb_qa 路径 1~3 次。
3. **失败必降级，不重试到底。** 每节点单次尝试 + 至多一次限时重试；超时直接走降级分支。
4. **上限写在 runner 里强制执行**，不靠提示词或模型自律。

### 6.2 节点契约（5 业务节点 + 2 确定性终止节点）

> 口径统一：**业务节点 5 个** = Router / RAG Retrieval / Troubleshooting / Evidence Checker / Final Answer；**确定性终止节点 2 个** = Refuse / Smalltalk。全文档（PRD、OpenAPI、README）一律用此口径，禁止出现"5 节点"或"6 节点"的模糊说法。

| 节点 | 类型 | 职责 | 输入 | 输出 | 模型调用条件 | 超时 | 失败处理 |
|---|---|---|---|---|---|---|---|
| **Router** | 规则优先 → LLM 兜底 | 判定意图：`kb_qa` / `troubleshooting` / `smalltalk` / `out_of_scope` | question、最近 **4 轮（8 条）**上下文 | `route`、`confidence`、`reason` | 命中规则（日志特征、明确排障动词、寒暄）→ **不调模型**；规则不确定才调 1 次 `qwen-flash`（JSON mode，≤64 tok） | 8s | 超时/JSON 解析失败 → 默认 `kb_qa`，`degraded=true` |
| **Query Rewrite**（Retrieval 内子步骤，**不单独成节点**） | 规则触发 → LLM 改写 | 把指代式追问改写成独立可检索 query（"那它怎么配？" → "Kubernetes Pod 探针怎么配置"） | question、最近 2~4 轮消息 | `rewritten_query` | **三重条件全部满足才调**：①`turn_index ≥ 1` ②命中指代词典（它/那/这个/还有/继续/上面说的…）③上文存在 assistant 回答。调 1 次 `qwen-flash`（≤64 tok） | 5s | 失败/超时/输出为空 → **用原 question 继续**，`degraded=true`，不阻断 |
| **RAG Retrieval** | **纯代码** | 用 `rewritten_query or question` 向量化 + 余弦 top-k + 相似度阈值 + 可用文档过滤 | query（改写后）、top_k、min_score | `chunks[]`（chunk_id、doc_id、score、原文片段） | 只调 **Embedding**（不产生生成调用） | 6s | Embedding 失败 → `chunks=[]` → 流向拒答分支 |
| **Troubleshooting** | LLM + 工具 | 结合片段与错误日志给出诊断步骤，可调 `analyze_error_log` / `check_port_conflict` | question、chunks、日志文本 | `diagnosis`、`tool_calls[]`、`confidence` | **仅** `route=troubleshooting` 且有日志/端口线索时调用；1~2 次。无日志则跳过 | **15s** | 超时/工具失败 → 降级为 RAG 回答并标注"未完成深度诊断" |
| **Evidence Checker** | **规则优先** → LLM 兜底 | ① 强制校验每个 citation 的 chunk_id 存在且属于本次检索集（代码）② 校验结论是否有片段支撑 | answer、citations、chunks | `supported`、`invalid_citations[]`、`unsupported_claims[]` | 规则判定"存疑"且答案含强断言时，调 1 次 `qwen-plus`（temp 0，≤256 tok） | 10s | LLM 失败 → 保守策略：删掉无支撑断言，保留有引用部分 |
| **Final Answer** | LLM | 组织最终回复、附引用列表、统一口吻 | 上游结论、citations、degraded | `final_text`、`citations[]` | 1 次 `qwen-plus`；若上游已达标且无非结构化内容 → **直通不调** | 15s | 失败 → 模板化兜底文本 + 已检索片段列表 |
| **Refuse**（终止节点） | **纯代码** | 生成标准拒答文案 + 说明已检索但无充分证据 | chunks、阈值 | `final_text` | 不调模型 | — | — |
| **Smalltalk**（终止节点） | **纯代码** | 寒暄/越界固定文案 + 引导语 | route | `final_text` | 不调模型 | — | — |

### 6.3 预算与终止条件（runner 强制）

```yaml
budget:
  max_steps: 8
  max_llm_calls: 6            # 全局硬上限，由 troubleshooting 最坏路径推导（见下表）
  reserved_for_final: 1       # 开局即锁定，任何情况不可被抢占
  max_tool_calls: 3
  wall_clock_deadline_ms: 60000
  per_node_timeout_ms: {router: 8000, rewrite: 5000, retrieval: 6000, troubleshoot: 15000, checker: 10000, final: 15000}
  single_retry: true          # 重试同样消耗预算
```

**逐路径最坏情况**（这张表是 6 的来源，任何改动必须先改这张表）：

| 路径 | router | rewrite | troubleshoot | checker(LLM 兜底) | final | 合计 |
|---|---|---|---|---|---|---|
| kb_qa 单轮 | 0~1 | 0 | — | 0~1 | 1 | **1~3** |
| kb_qa 指代追问 | 0~1 | 1 | — | 0~1 | 1 | **2~4** |
| troubleshooting | 1 | 0~1 | 1~2 | 0~1 | 1 | **3~6** |
| no_doc 拒答 | 0~1 | 0~1 | — | 0 | 0（Refuse 纯代码） | **0~2** |
| smalltalk | 0~1 | 0 | — | 0 | 0 | **0~1** |

> 全局 6 由 troubleshooting 的最坏路径决定。v3 写的 4 只覆盖了 kb_qa，故障排查路径（1+2+1+1=5）必被砍 —— 砍的恰好是 Final，等于在最亮眼的用例上退化成模板兜底。**这是设计错误，不是调参问题。**

**预算抢占优先级**（预算或时间紧张时按序降级，Final 永不降级）：

| 优先级 | 调用 | 预算不足时的行为 |
|---|---|---|
| **P0 保留** | Final Answer | **永不被抢占**（开局锁定 1 次），保证任何路径都有真实生成结果 |
| P1 必保 | Router | 仅在完全无余量时跳过 → 默认 `kb_qa` |
| P1 必保 | Troubleshooting 第 1 次 | 保 1 次；第 2 次可弃 |
| P2 可弃 | Troubleshooting 第 2 次（工具结果复述） | 直接用第 1 次结论收尾 |
| P3 可弃 | Checker 的 LLM 兜底 | 退回纯规则保守策略（删无支撑断言） |
| P4 可弃 | Query Rewrite | 用原 question 检索 |

**时间兜底**：逐节点超时之和 = 8+5+6+15+10+15 = **59s ≤ 60s 墙钟**。（v3 的 troubleshoot=20s 会让总和达 64s > 60s，墙钟必然打断最后一个节点，已改为 15s。）

**超时/超预算的处理**：不中断整轮，按上表降级；`trace.terminated_reason ∈ {deadline_exceeded, budget_exhausted, step_limit}`，`trace.preempted[]` 记录被跳过的节点，`degraded=true`。

### 6.4 典型路径与调用次数

| 路径 | 节点序列 | 生成调用数（与 §6.3 最坏情况表一致） |
|---|---|---|
| 知识问答 · 单轮 | Router → Retrieval → Evidence(规则) → Final | **1~3** |
| 知识问答 · 指代追问 | Router → (Rewrite) → Retrieval → Evidence → Final | **2~4** |
| 故障排查 | Router → (Rewrite) → Retrieval → Troubleshooting → Evidence → Final | **3~6** |
| 无证据拒答 | Router → (Rewrite) → Retrieval(空) → Refuse | **0~2** |
| 寒暄/越界 | Router → Smalltalk | **0~1** |

> 区间上限全部落在 `max_llm_calls=6` 之内；故障排查路径即使触发 rewrite + 2 次诊断 + checker 兜底，Final 仍有预留额度可用。

### 6.5 State 关键字段

`question` / `session_id` / `turn_index` / `rewritten_query` / `route` / `retrieved_chunks` / `tool_results` / `draft_answer` / `citations` / `check_result` / `final_text` / `degraded` / `terminated_reason` / `usage{llm_calls, tool_calls, tokens}`

---

## 7. 文档摄取：状态机与幂等（v4.3 按 Q1–Q6 决策重写）

> 详细协议见 `docs/architecture/ingest-consistency.md`，DDL 见 `docs/architecture/data-model.md`，接口语义见 `docs/api/ingest-contract.md`。本节只保留结论。

### 7.1 状态机

五个状态：`pending | processing | completed | failed | deleting`

```
                     watchdog: pending 超 120s
                     ┌──────────────────────────► failed(DISPATCH_FAILED)
                     │
上传 → pending ──claim──► processing ──发布成功──► completed
                              │
                              ├── Python 返回失败 ──► failed(PARSE_ERROR / EMBEDDING_FAILED / …)
                              └── watchdog: 超 150s ─► failed(TIMEOUT)

failed ──reingest──► pending        （attempt 在下次 claim 时 +1）
任意状态 ──delete──► deleting ──清理完成──► 行删除
```

- 状态真相源：`ingest_jobs.status`，**只由 Go 写**（Python 只读 `current_run_token` / `attempt`）。
- `attempt` 表示已执行次数：建行默认 **0**，每次抢占 +1（首次执行后为 1），仅作执行世代标识；**V1 不自动重试**。
- 任务级 `error_code`：`DISPATCH_FAILED` / `TIMEOUT` / `PARSE_ERROR` / `EMPTY_CONTENT` / `EMBEDDING_FAILED` / `RATE_LIMITED` / `DB_ERROR`。
- 检索强约束：`doc_chunks.visible=TRUE` **且** `ingest_jobs.status='completed'` **且** `documents.deleted_at IS NULL` **且** `owner_id` 匹配。

### 7.2 唯一标识与重复上传

- `documents.source_hash = sha256(规范化后全文)`，**仅用于同一 owner 内去重**；索引为 `(owner_id, source_hash) WHERE deleted_at IS NULL`，**不得全局唯一**（否则跨用户命中对方 `doc_id`，属信息泄露）。
- 重复上传 → **202** + 既有 `doc_id` + `deduplicated: true`（与新建同码），不触发二次摄取；若既有文档为 `failed`，附带 `retry_hint`。
- 内容变更的同名文件 = 新 hash = 新文档（V1 不做版本链）。

### 7.3 摄取与发布（两阶段）

1. Go 抢占：`pending → processing`，同时 `attempt+1`、生成 `run_token`、记 `started_at`（单条条件更新）。
2. Go 调 Python `POST /internal/v1/ingest`（连接 3s / 读 120s），Python 完成解析、切片、Embedding，写 `doc_chunks` 时 **`visible=false` 且带 `run_token`**，**不写 `ingest_jobs`**。
3. Go 发布（单事务）：`run_token` CAS → `completed` + 本世代 `visible=true` + 删除旧世代切片。
4. 失败：CAS → `failed(error_code)`，并清本次 `visible=false` 切片。

要点：
- **不可见切片永不参与检索**，发布是唯一生效闸门；旧执行的 token 已被覆盖，CAS 必然 0 行 → 结果丢弃（审计 `stale_result_dropped`）。
- **429 实测不含 `Retry-After`**（仅 `code=limit_requests`）：退避必须自定义（1s/2s/4s，最多 3 次），不能依赖服务端提示。
- V1 **不做断点续传**：≤5MB 文档重跑成本可接受。

### 7.4 删除（可恢复）

先阻断（`deleting`）→ 按 `doc_id` 清全部向量（含 `visible=false`）→ 最后删 `documents` / `document_contents` / `ingest_jobs`。

- **清理未完成前不得删 `documents` 行**，否则遗留无主向量。
- `deleting` 期间拒绝检索、`reingest`、读正文；重复删除 202 幂等。
- 清理失败由 watchdog 每 60s 重试，状态停留 `deleting`。

### 7.5 写入权限矩阵（单表单写者）

| 表 | 写入方 | 读取方 |
|---|---|---|
| `documents` / `document_contents` | **Go 独占** | Go、Python（正文只读） |
| `ingest_jobs` | **Go 独占**（含全部状态迁移） | Python 只读 token / attempt |
| `doc_chunks` | **Python 写内容（visible=false）**；**Go 只改 visible 与按 token 清理** | 双方各读所需 |
| `sessions` / `messages` / `agent_runs` / `tool_calls` | Go 独占 | Go、Python（上下文只读） |

## 8. 工具安全边界

### 8.1 V1 三个工具（纯函数，不触达主机、不执行命令）

| 工具 | 入参 | 出参 | 实现约束 |
|---|---|---|---|
| `analyze_error_log` | `log_text`（≤8KB）、`language?` | 错误类型、关键行号、可疑时间戳、错误码提取、分组统计 | 正则 + 规则匹配，**不调模型、不执行命令**；超长截断并标注 |
| `check_port_conflict` | `ports`（1~20 项 `{port, protocol, service?, pid?}`）、`expected_owner?` | 冲突判定、占用者、建议结论 | 只分析**用户提供的模拟数据或明确限定输入**，**不调用 `netstat`/`ss`**；端口 1~65535 校验 |
| `search_knowledge_base` | `query`（≤500 字）、`top_k`(1~10，默认 5) | 片段 + doc_id + chunk_id + score | 内部走 §6.2 检索；受 `completed` 约束 |

### 8.2 硬性禁令（代码强制，不靠提示词）

1. **不存在**通用执行器：无 `exec`、`subprocess`、`os.system`、`eval`、动态 SQL 拼接。
2. 模型输出的任何字符串**不得**作为命令、SQL、路径使用；工具入参一律 Pydantic 校验 + 白名单枚举。
3. 工具注册表是**封闭集合** `TOOL_REGISTRY = {3 个函数}`；调用名不在集合内 → `ToolNotAllowed` → trace 记录 → 返回明确错误。
4. 路径类参数在 V1 **完全不存在**（没有读文件的工具）。
5. CI 加静态检查：`app/tools/` 下出现 `subprocess` / `os.system` / `exec(` 即失败。

### 8.3 执行限制

每工具超时 5s；单轮最多 3 次工具调用；输出统一截断 4KB；每次记录 `tool_name / args_digest / duration_ms / status`。

---

## 9. RAG 评测规范

### 9.1 指标定义

| 指标 | 公式 | 说明 |
|---|---|---|
| **Recall@5** | `Σ|Top5 ∩ Gold| / Σ|Gold|`（micro 平均） | Gold = 标注的相关 chunk 集合 |
| **MRR@5** | `(1/N)·Σ(1/rank_i)`，Top5 内无相关计 0 | 衡量排序质量 |
| **引用 ID 有效率** | `有效 ID 引用数 / 总引用数` | 有效 = ①`chunk_id` 存在 ②属于本次检索集。**自动断言，目标 100%**，不靠人工 |
| **引用内容支持率** | `被支撑的引用数 / 抽检引用数` | ③该片段确实支持该句结论。人工抽检 20 条，目标 ≥ 0.90 |
| **引用支持率（复合口径，已弃用）** | — | v4 把"ID 存在"与"内容支持"合成一个"引用有效率"，会掩盖"引用都存在但都不支持结论"的情况。v1.2 起拆为上面两项，本行仅作历史说明 |
| **拒答正确率** | `正确拒答数 / 应拒答样本数` | 仅 `should_answer=false` 子集；同时报**过度拒答率** = 应作答却被拒 / 应作答样本数 |
| **任务成功率** | `成功样本数 / 总样本数` | 端到端人工判定（答案正确 + 引用有效 + 无幻觉），二值 |
| **多轮保持率** | `Recall@5(多轮追问) / Recall@5(同义单轮对照)` | 仅 `multiturn` 子集，按配对样本计算；**< 1.0 说明改写有害**，> 1.0 说明改写补齐了指代信息 |
| **延迟** | P50 / P95（ms） | 分三段：检索、LLM 生成、端到端 |

### 9.2 数据集格式（`evals/datasets/qa_golden.jsonl`，每行一条）

单轮样本：

```json
{"id":"q001","category":"kb_qa","question":"Pod 一直 CrashLoopBackOff 怎么排查？","should_answer":true,"gold_doc_ids":["doc_abc"],"gold_chunk_texts":["...原文片段..."],"reference_answer":"...","notes":"覆盖镜像拉取失败与探针配置"}
```

多轮样本（**必须有配对字段**，否则"不劣于"无法测）：

```json
{"id":"m001","pair_id":"p001","category":"multiturn","context_turns":[{"role":"user","content":"K8s Pod 一直重启怎么排查？"},{"role":"assistant","content":"先看 describe 事件的 Last State，若是 OOMKilled 则..."}],"question":"那它怎么配？","control_single_turn_question":"Kubernetes Pod 内存 limit 和探针怎么配置？","should_answer":true,"gold_chunk_texts":["...同一批 Gold 片段..."],"notes":"指代'它'=内存 limit；对照句为改写目标"}
```

字段约束：
- `category ∈ {kb_qa, log_diag, no_doc, ambiguous, citation_risk, multiturn}`
- `context_turns` 仅在 `multiturn` 出现，长度 2~4 条，按时间顺序。
- `control_single_turn_question` 仅在 `multiturn` 出现，语义等价于"改写后的理想 query"，且 **Gold 必须与多轮样本完全一致**，否则 A/B 不可比。
- `pair_id` 用于把多轮样本与对照组配对统计。
- 用 `gold_chunk_texts` 定位 chunk（重新摄取后 id 会变，文本更稳定）。

### 9.3 35 题分布

| 类别 | 数量 | 目的 |
|---|---|---|
| 普通知识问答（kb_qa） | 10 | 主路径命中率 |
| 故障日志（log_diag） | 8 | Troubleshooting 路径与工具调用 |
| 无相关文档（no_doc） | 6 | 拒答正确率（不得编造） |
| 模糊问题（ambiguous） | 3 | 澄清或保守作答 |
| 错误引用风险（citation_risk） | 3 | Evidence Checker 拦截能力 |
| **多轮追问（multiturn）** | **5** | 指代改写有效性（含 5 条同义单轮对照） |
| **合计** | **35** | 其中 5 条为多轮，另有 5 条对照问法一并执行 |

> v3 承诺了"5 条多轮样本"但分布表没有位置、schema 也没有承载对话历史的字段 —— 属前后不一致，v4 已补齐。选择 30→35 而非从 kb_qa 切 5 题，是为了不削弱主路径样本量（kb_qa 只有 10 条，切 5 条会让主路径统计失去意义）。

### 9.4 执行与真实性约束

- 入口：`python -m evals.run_eval --dataset evals/datasets/qa_golden.jsonl --out evals/reports/{ts}.json`
- 报告必须含：数据集版本、git commit、模型名、参数、逐题结果、汇总指标、运行时间。
- **未执行的指标一律输出 `NOT RUN`，禁止填充推算值或示例值。**
- 阈值（V1 目标，首次真实运行后据基线校准）：

| 指标 | 阈值 |
|---|---|
| Recall@5 | ≥ 0.75 |
| MRR@5 | ≥ 0.60 |
| 引用内容支持率 | ≥ 0.90（人工抽检 20 条） |
| 引用 ID 有效率 | **100%**（自动断言，不进阈值表，属硬性正确性） |
| 拒答正确率 | ≥ 0.85 |
| 任务成功率 | ≥ 0.70 |
| 多轮保持率 | **≥ 0.90**（允许 10% 衰减，低于此说明 rewrite 该关掉） |
| 端到端 P95 · kb_qa 子集 | ≤ 8s（单轮问答主路径） |
| 端到端 P95 · log_diag 子集 | ≤ 25s（含工具调用与多步诊断） |
| 检索 P95 | ≤ 800ms（不含模型生成） |

> 说明（v4.1 修正）：v3/v4 曾写"整体 P95 ≤ 15s"。35 题里 8 条 log_diag 单项就可能 20s+，混算后的 P95 必然被这 8 条拉爆，指标本身不可达 —— 改为按路径分别设阈值。整体 P95 仍作为**观察指标**上报，不设硬阈值。

### 9.5 多轮 A/B 对照执行方法

对 `multiturn` 子集的每条样本，跑两次并配对比较：

| 运行 | 输入 | 期望 |
|---|---|---|
| A（多轮） | `context_turns` + `question`（"那它怎么配？"） | 触发 Query Rewrite |
| B（对照） | 仅 `control_single_turn_question`（"Kubernetes Pod 内存 limit 和探针怎么配置？"） | **不触发** rewrite（首轮 + 无指代词） |

计算：
- `Recall_A` = A 组在 `gold_chunk_texts` 上的 Recall@5
- `Recall_B` = B 组同指标
- `多轮保持率 = Σ Recall_A / Σ Recall_B`（分子分母均为通过有效性检查的配对样本之和，**不是逐对相除再平均**）

**除零与失效配对规则（v4.1 补）**：
1. `Recall_B = 0` 的配对**剔除**，不折算、不代入 1.0、不跳过统计。对照问法自己都召不回 Gold，说明该题标注或检索本身有问题，比值无意义。
2. 剔除的配对在报告里单列 `NOT_COMPARABLE`，并注明原始 `Recall_A` 与 `Recall_B`，便于人工回看。
3. 若 `NOT_COMPARABLE` 超过 5 条中的 2 条（>40%），说明数据集不可用：**多轮保持率一律输出 `NOT RUN`**，同时把该情况作为数据集缺陷写入报告，不允许用剩余 3 条凑一个好看的数字。
4. `Σ Recall_B = 0` 时直接输出 `NOT RUN`，不输出 `Infinity` 或 0。

判定：
- `≥ 0.90` 通过；
- `< 0.90` 视为改写引入噪声，先调 prompt 与触发词典，仍不达标则**关闭 rewrite，并在 PRD 中把多轮标注为已知限制** —— 不在文档里含糊其辞。
- B 组若也触发了 rewrite，说明触发条件判定有 bug，直接判该样本失败（这样能同时测到"首轮不复写"）。

---

## 10. 健康检查设计

| 端点 | 语义 | 行为 |
|---|---|---|
| `GET /health/live` | 进程存活 | **永远 200**，不查依赖（liveness） |
| `GET /health/ready` | 可对外服务 | 检查 PG 连接、迁移版本、provider 最近探测结果；任一失败 → **503**（readiness） |
| `GET /health/deps` | 依赖明细 | `{db:{status,latency_ms}, llm:{status,latency_ms,model}, embedding:{status,latency_ms,dim}}`，不返回 Key |

**降级原则**：
1. 模型不可用**不阻止进程启动**（仅配置缺失/非法才 fail-fast 退出）。
2. provider 状态由后台探测器每 30s 刷新并缓存，**不在每次业务请求时打模型**。
3. 依赖不可用时业务请求返回 `503` + 明确错误码（`MODEL_UNAVAILABLE` / `EMBEDDING_UNAVAILABLE` / `DB_UNAVAILABLE`）。
4. Go 的 `/health/ready` 汇总自身 DB + Python 服务 + Python 上报的依赖状态；Python 挂掉不影响 Go 的 `/health/live`。

### 10.1 Go ↔ Python 内部鉴权（v2 缺失，已补）

**方案：shared secret header，不做 mTLS、不做 OAuth。**

```
X-Internal-Token: <AI_SERVICE_TOKEN>
X-Request-Id:     <与 Go 侧一致的 trace id，用于跨服务串联>
```

| 项 | 规定 |
|---|---|
| 令牌来源 | `.env` 的 `AI_SERVICE_TOKEN`（≥32 字节随机串）；`.env.example` 只放占位符；仓库不含真值 |
| 校验位置 | Python 侧**全局中间件**，覆盖 `/ingest`、`/retrieve`、`/chat`、`/tools/*`；`/health/*` **豁免**（供编排探活） |
| 比较方式 | `hmac.compare_digest` 常量时间比较，防时序侧信道 |
| 失败响应 | `401` + `INTERNAL_AUTH_FAILED`，**不回显任何令牌信息**，不区分"缺失/错误" |
| 网络暴露 | AI 服务**不映射宿主端口**（仅 compose 内网）；本机调试例外映射 `127.0.0.1:8000`（仍校验令牌） |
| 启动校验 | `AI_SERVICE_TOKEN` 缺失或长度不足 → Python fail-fast 退出（这是唯一允许因配置退出的场景） |
| 日志 | 禁止打印令牌；请求日志只记录 `token_fp`（sha256 前 8 位） |
| 契约 | `openapi-ai.yaml` 必须声明 `securitySchemes.internalToken = {type: apiKey, in: header, name: X-Internal-Token}`，每个内部端点引用 |
| 测试 | 缺失令牌 → 401；错误令牌 → 401；正确令牌 → 200；三种情况都必须有自动化用例 |

> 为什么不做 IP 白名单：compose 网络中容器 IP 会变，白名单维护成本高于收益。为什么不复用用户 JWT：内部端点不是用户身份语义，混用会让权限模型变脏。

---

## 11. 技术风险与应对（按严重度降序）

| 排序 | 风险 | 影响 | 应对 |
|---|---|---|---|
| 1 | **范围膨胀导致延期**（Agent + 前端 + 评测 + 部署同压） | 交不出可运行 V1 | §3 范围冻结；V1 后端闭环优先；前端 V1.1；阶段验收不过不推进 |
| 2 | **网关模型漂移**：`/models` 返回 178 个第三方模型，说明是网关不是纯百炼目录；模型可能下线改名 | 线上突然 404 | 模型名全配置化；启动探测；缺失时按 max→plus→flash 映射降级并标 `degraded` |
| 3 | **检索质量不足** → 拒答过多或幻觉 | 演示差、评测不达标 | 先建 35 题基线再调参；切片/重叠/阈值配置化；若 Recall@5 < 0.75 再引入 rerank |
| 4 | **多轮指代检索失效**（"那它怎么配？"直接 embedding 必然差） | 多轮问答答非所问，演示直接翻车 | §6.2 条件式 Query Rewrite + 5 条多轮样本进评测；改写失败用原句不阻断 |
| 5 | **引用悬挂**：删文档后历史消息引用失效 | 查历史 500 或空白，显得系统不稳 | JSONB 快照 + `live/deleted/orphaned` 三态 + 专项测试（§7.7） |
| 6 | **Go/Python 契约漂移** | 联调返工 | 两份 OpenAPI 先行 + 契约测试（阶段 1 最小闭环） |
| 7 | **pgvector 未验证** | 数据模型可能返工（维度/索引） | 阶段 1 第一件事就验；维度 1024 已实测，HNSW 待验 |
| 8 | **内部接口无鉴权 → 裸奔**（v2 漏洞） | 同网段任意调用，烧额度/写脏数据 | `X-Internal-Token` + AI 服务不映射宿主 + 三种鉴权用例（§10.1） |
| 9 | 429 限流（批量上限 10 已实测） | 入库中断 | 分批 ≤10 + 退避重试；并发上限配置化 |
| 10 | 磁盘 31G + 构建缓存 | 构建失败、镜像堆积 | slim 基底；`builder prune` 入 Makefile |
| 11 | Docker Hub 不可直连 | 无法新增镜像 | 全部用本地已有镜像；新依赖走 pip 而非新镜像 |
| 12 | 中文路径 + 文件名编码 | 上传乱名、日志乱码 | NFC 规范化 + 全链路 UTF-8 + 文件名白名单 |
| 13 | GOSUMDB 直连不通 | `go mod tidy` 失败 | 阶段 1 实测，必要时局部调整 |
| 14 | **呈现层缺失**：技术全对但没人看 | 求职失效，等于白做 | §3.4 把 README/脚本/GIF 列为 S8 硬验收 |

---

## 12. 阶段 1 前置检查清单（SPIKE）

按顺序执行，每项记录"命令 / 预期 / 实际 / 结论"。任一项失败 → 先改设计，不改代码。

### 12.1 Go 基础服务
- [ ] `go run ./cmd/api` 起得来，`GET /health/live` 返回 200
- [ ] `go mod tidy` 成功（验证 GOSUMDB 处理方案）
- [ ] `go test ./...` 可执行（允许 0 用例）

### 12.2 Python 基础服务
- [ ] 用 3.11.9 建 venv，`uvicorn app.main:app` 起得来
- [ ] `GET /health/live` 200；`/health/deps` 结构正确
- [ ] `pytest` 可执行

### 12.3 PostgreSQL + pgvector
- [ ] 独立容器（宿主 55433）起得来；`CREATE EXTENSION vector` 成功
- [ ] 建 `vector(1024)` 表，插 3 条，余弦 top-k 返回正确顺序
- [ ] HNSW 索引创建成功
- [ ] **partial 索引验证（新协议特有形态）**：在 `CREATE INDEX ... USING hnsw (embedding vector_cosine_ops) WHERE visible = TRUE` 上，先插入 `visible=false` 的向量（不应被索引命中），再 `UPDATE visible=TRUE`（应立即可被 top-k 检索命中）。这条验证"发布翻转"与 partial 索引的交互，是 `run_token` + `visible` 协议的必要前提
- [ ] 迁移脚本可重复执行（幂等）

### 12.4 百炼
- [ ] 聊天最小请求 200（`qwen-flash`）✅ 已验证
- [ ] Embedding 最小请求 200，dim=1024 ✅ 已验证
- [ ] 401 / 404 / 400（批量超限）响应结构固化进错误映射表 ✅ 已有样本
- [ ] 429 真实响应形态（待测）
- [ ] 客户端超时与重试策略落地为代码常量

### 12.5 跨服务契约
- [ ] Go → Python 最小 JSON 往返（含中文）
- [ ] 超时透传、错误码透传验证
- [ ] Python 不可用时 Go 返回 503 且不崩
- [ ] **内部令牌三态**：缺失 → 401、错误 → 401、正确 → 200；`/health/*` 无令牌仍可访问

### 12.6 通过标准
全部打勾 + 产出 `docs/testing/spike-report.md`（含实际输出片段），**才允许进入阶段 2**。

---

## 13. 开发顺序与验收标准（V1）

| 阶段 | 内容 | 交付物 | 验收标准 |
|---|---|---|---|
| **S0 规划**（本阶段） | 环境、范围、决策、风险 | 本文档 | 用户审核通过 |
| **S1 SPIKE** | §12 全部验证 | `spike-report.md` | 12.1–12.6 全绿；设计按结果修正 |
| **S2 契约** | PRD、数据模型、两份 OpenAPI、graph 设计、工具策略、评测规范 | docs 全套 | 无 TBD；DDL 可执行；OpenAPI lint 通过 |
| **S3 骨架** | 仓库初始化、两侧骨架、compose、迁移、日志、错误码、健康检查 | `make up` 可跑 | 三件套起来；`/health/{live,ready,deps}` 语义正确；依赖挂掉 ready=503 但 live=200 |
| **S4 摄取** | 上传→解析→切片→Embedding(1024)→入库 | `POST /documents`、`/ingest` | 5 篇成功；重复上传 `deduplicated=true` 且不重复计费；元数据完整；非法文件错误码明确；状态机可观测；**未完成文档不参与检索（有测试）**；P95 < 30s |
| **S5 RAG** | 检索 + 生成 + 引用 + 拒答 + **条件式改写** | 消息接口（非流式） | 引用 chunk_id 真实存在且有断言；无证据必拒答；Evidence 规则层拦无效引用；**指代追问（"那它怎么配？"）能检索到正确片段，且首轮不复写（有测试）**；P95 < 8s |
| **S6 Agent** | **5 业务节点 + 2 终止节点** + 预算 + 轨迹 | graph 实现 | Router 10 条集 ≥90%；**规则节点确实不调模型（用 mock 计数断言）**；`max_llm_calls=6` 生效；**构造预算吃紧的故障排查用例，断言 `final` 仍被真实调用、`preempted` 不含 final**；超时降级生效；每节点 trace 完整；`terminated_reason` / `preempted[]` 有值可查 |
| **S7 工具** | 3 个纯函数工具 | `app/tools/` | 非白名单调用被拒（有测试）；注入用例（超长、非法端口、SQL 片段、`../`）全拦；超时/截断生效 |
| **S8 评测与交付** | **35 题评测**（含多轮 A/B）、测试补齐、部署文档、**呈现层** | 评测报告 + README + demo 脚本 + GIF | pytest / `go test` 全绿；端到端集成测试通过；7 项指标真实产出（未跑标 NOT RUN），其中**多轮保持率 ≥ 0.90**；干净环境到冒烟 ≤5 分钟；**§3.4 四件套齐备**（README 亮点区 / 3 分钟脚本含拒答案例 / 真实录屏 / pitch 三条 bullet） |
| **S9 前端**（V1.1） | 轻量单页 + SSE 流式 | `web/` | 通过 PRD §11.5 全部验收：U1–U10 逐条冒烟 + 五功能可用（聊天含流式/上传/引用/历史/轨迹）+ 视觉底线（同栅格、单一强调色、无裸控件、三态文案）；无构建链。<br>**⚠️ 前置行动项 `S9-PRE-1`（来源 = S8 A3b 人工复核实证，见 `docs/testing/a3b_user_review.md` 与报告 v2 校准日志）**：<br>**引用纪律收紧** —— `m001` 实测发现模型把**片段之外的通用知识挂在 KB 引用号下**（引用 ID 真实有效，但内容不支撑结论；机器指标全抓不到，仅人工复核发现）。<br>**要求**：`final` / `troubleshoot` 的 prompt 收紧 —— **只复述片段内的操作建议**；超出片段的知识**不带引用号**，或显式标注「模型通用知识，无 KB 出处」。<br>**验收**：修复后重跑 `m001`（多轮"那它怎么配？"），其引用片段必须能逐句支撑结论；并复跑 A3b 5 条确认支持率上升（目标 1.000） |

---

## 14. 与 v1 的差异说明

| # | v1 原方案 | v2 修订 | 原因 |
|---|---|---|---|
| 1 | LLM 用 DeepSeek | **全量改百炼** | 用户指令 |
| 2 | Embedding 未定（本地 fastembed / 远程），默认 512 维 | **定 `text-embedding-v4`，1024 维（实测）** | 用户定供应商；省掉 ONNX/torch 与磁盘风险 |
| 3 | Router 用 `qwen-turbo` | 改 **`qwen-flash`** | 实测清单里**没有裸 `qwen-turbo`**，只有日期版本；`qwen-flash` 实测 0.45s |
| 4 | 5 节点默认都调模型 | **混合节点**：Retrieval 纯代码、Evidence 规则优先、Final 可直通；硬预算 6 次 | 收敛设计；降本、降延迟、降不确定性 |
| 5 | 无步数/超时/预算定义 | `max_steps=8`、`max_llm_calls=6`、`max_tool_calls=3`、60s 墙钟、逐节点超时 | 防无限重试与高成本调用 |
| 6 | 工具"3~5 个假想工具"（`read_log_tail`、`check_disk_usage` 等含主机读取暗示） | **收敛为 3 个纯函数**：`analyze_error_log` / `check_port_conflict` / `search_knowledge_base`，不触达主机 | 安全边界；模型输出不得成为命令 |
| 7 | "本项目不做前端" | V1 不做，**V1.1 做轻量单页**（聊天/上传/引用/历史/轨迹） | 用户新增要求，需要演示载体 |
| 8 | 文档状态未定义 | `pending→processing→completed/failed` + 幂等 + 重试 + 删除清理 + 单写者矩阵 | 避免重复切片与脏数据 |
| 9 | 指标 `Hit@3 / 引用准确率 / 拒答正确率` | 统一 **Recall@5、MRR@5、引用有效率、拒答正确率、任务成功率、多轮保持率、延迟** + 公式 + 数据格式 + 35 题分布 + NOT RUN 规则 | 指标可复算、可对比 |
| 10 | 只有两个 `/health` | 拆 **live / ready / deps** 三级 + 降级原则 | 容器语义清晰；模型挂不影响启动 |
| 11 | "先写大量架构文档再写代码" | 插入 **S1 SPIKE**，6 组验证全绿才写契约与代码 | 避免未验证假设进入主体实现 |
| 12 | "百炼 Key 未配置"为阻塞项 | 改为**已实测通过**（鉴权/模型/维度/批量/错误码），剩余 429 与超时待测 | 实际请求结果 |
| 13 | 无范围冻结 | 新增 §3 必须交付 / V1.1 / 明确不做 三张清单 | 控制范围 |
| 14 | 风险只有环境风险 R1–R8 | 拆为**环境风险（§1.8）**与**技术风险（§11，按严重度排序）** | 严重度可排序、可跟踪 |

### 14.1 未改动部分

环境实测数据（§1）、目录结构主体、文档清单骨架、端口与镜像策略、单写者思路、不引入 Redis/K8s/Kafka 的约束——全部保留。

### 14.2 v2 → v3 修订（回应复核意见 6 条）

| # | 复核意见 | 采纳 | 处置 |
|---|---|---|---|
| 1 | 原文存储位置未定义，是 P0 洞 | **全部采纳** | 新增 §7.6：原文进 PG 独立表 `document_contents`（1:1，`ON DELETE CASCADE`），明确不用共享 volume，并列出五种维度对比、列表查询禁 join、5MB 上限、`source_hash` 基于规范化文本 |
| 2 | 多轮指代没有消解，检索必然差；要么加改写要么明说不承诺 | **选"加改写"** | §6.2 新增 Query Rewrite 子步骤（不新增节点）：三重条件同时满足才调 1 次 `qwen-flash`，失败用原句不阻断；预算仍为 4 次；§3.1 增加第 11 条验收项与 5 条多轮评测样本 |
| 3 | "5 节点"与表格 6 行矛盾 | **全部采纳** | 全文档统一口径：**5 业务节点 + 2 确定性终止节点**；§6.2 顶部加硬性口径声明，禁止再出现模糊说法；§3.1、§13 同步 |
| 4 | 删除文档后历史引用悬挂无语义 | **全部采纳** | 新增 §7.7：`messages.citations` 用 JSONB 快照，返回 `live/deleted/orphaned` 三态；硬性要求"不得 404/500"；快照 200 字、不承诺全文回看；删除语义补级联说明 |
| 5 | 内部鉴权只字未提 | **全部采纳** | 新增 §10.1：`X-Internal-Token` shared secret + `hmac.compare_digest` + `/health/*` 豁免 + AI 服务不映射宿主 + 三种鉴权用例 + OpenAPI securityScheme |
| 6 | 缺呈现层，求职没人读计划书 | **全部采纳并升为硬验收** | 新增 §3.4 四件套（README 亮点区 / 3 分钟脚本含拒答案例 / 真实录屏 / pitch 三条 bullet），写进 S8 验收；§11 新增风险 14 |

**另修的隐性矛盾**（复核时顺带发现）：
- v2 说 Refuse 是节点但又只数 5 个 → 已按第 3 条统一。
- v2 的 §7.5 删除语义没提 `document_contents` 与历史消息 → 已补齐。
- v2 的 §3.1 未覆盖"多轮追问"与"引用悬挂"两项能力 → 已补为第 11、12 条。

### 14.3 v3 → v4 修订（回应复核意见 2 条 + 自查发现 1 条）

| # | 复核意见 | 判断 | 处置 |
|---|---|---|---|
| 1 | `max_llm_calls=4` 只在 kb_qa 路径成立；故障排查最坏 6（含 rewrite）/ 5（不含），runner 会砍掉 Final，在最亮眼的用例上退化为模板兜底 | **成立，是设计错误而非调参问题** | 采纳"提到 6 + 逐路径表"方案：`max_llm_calls=6`（由 troubleshooting 最坏路径推导）；§6.3 新增逐路径最坏情况表替代原注释；§6.4 数字同步（kb_qa 1~3 / 指代 2~4 / 故障 3~6 / 拒答 0~2 / 寒暄 0~1）。**另加 `reserved_for_final=1` 开局锁定 + 四档抢占优先级**，解决"预算一到就牺牲 Final"的根因，而不只是把上限改大 |
| 2 | §3.1 承诺 5 条多轮样本，但 §9 没动：category 无多轮类、30 题分布无位置、schema 无对话历史字段、"不劣于"不可测 | **成立，前后不一致** | 采纳"30→35 加一类"（不切 kb_qa，因为只有 10 条，切 5 条会让主路径统计失去意义）；§9.2 增 `context_turns` / `control_single_turn_question` / `pair_id`；§9.3 加 `multiturn` 5 条；§9.1 新增**多轮保持率**指标（`Recall_A / Recall_B`，阈值 ≥ 0.90）；§9.5 定义 A/B 配对执行方法，并加"B 组若也触发 rewrite 直接判失败"以顺带测到首轮不复写 |
| 3 | （自查，复核未提）逐节点超时之和 8+5+6+20+10+15=**64s > 60s 墙钟**，故障排查路径必然被墙钟打断最后一个节点 | 成立 | `troubleshoot` 超时 20s → **15s**，总和 59s ≤ 60s；并在 §6.3 标注该等式，防止后续调参再次破坏 |

**连带同步**：§2.1 新增 D14/D15；§3.1 第 8 条改 35 题、第 11 条改为可测口径；§5 文档清单的 rag-eval-spec 验收项改 7 项指标 + 35 题 + A/B；§13 的 S6 增加"预算吃紧时 final 仍被调用"的断言、S8 改 7 项指标与多轮保持率；§11 风险 3 的基线题量同步 35。

---

## 附：本阶段执行记录

**文档维护规则（2026-10-22 补充，起因：同一处数值连续两轮漏改）**：

1. 改数值/枚举/状态名后，**必须回读所有引用该值的位置**（`grep` 关键字 + 章节交叉引用），而非只改当前章节。
2. 写变更记录前，**先核对被改位置的实际内容**；变更记录里说"已改"但正文没改，比不改更糟——它会让下一个读者（包括我自己）放弃检查。
3. 跨文档引用的口径（状态枚举、错误码、超时值、去重语义）以**单一权威文件**为准，其余文档只引用不复述。当前权威分配：HTTP 错误码 → PRD §9.2；任务状态与协议 → `architecture/ingest-consistency.md`；DDL → `architecture/data-model.md`；接口语义 → `api/ingest-contract.md`。
4. **文档状态行只允许外部审查通过后由审查结论变更；实现方不得自行将"待审查"改为"已冻结"**。状态是审查意志的记录，不是实现进度标签。

**已做**：只读环境探测；百炼最小请求验证（聊天/Embedding/维度/批量上限/401/404/JSON mode/流式，约 20 次请求）；文档 v2 修订；按复核意见出 v3（原文存储、多轮改写、节点口径、引用悬挂、内部鉴权、呈现层）；再按复核意见出 v4（预算算术修正、多轮评测承载、超时总和自查）。
**未做**：未 `git init`；未安装依赖；未拉取镜像；未启动容器；未写业务代码；未修改系统环境；未将 Key 写入任何文件。
**待办**：pgvector 写入检索、429 形态、超时行为、Go↔Python 契约 —— 见 §12，阶段 1 执行。
