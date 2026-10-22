# S1 SPIKE 实测报告

| 项 | 值 |
|---|---|
| 日期 | 2026-10-22 20:05 – 20:20 (GMT+8) |
| 执行人 | 开发侧（AI 辅助） |
| 依据 | `docs/00-planning/00-environment-and-plan.md` §12（阶段 1 前置检查清单） |
| 结论 | **S1 通过（含 3 项实测触发的设计修订，已回写）**，但 HNSW 召回与真实语料待 S4 复测 |
| 执行范围 | 仅 health 级骨架（Go `/health/live`、Python `/health`、内部 echo/slow/boom），**无业务代码** |

> **脱敏声明**：本报告出现的凭据一律打码：`DASHSCOPE_API_KEY=sk-***`、`AI_SERVICE_TOKEN=***`、`POSTGRES_PASSWORD=***`。所有实际 Key/Token 均从 `deploy/.env` 读取，从未打印。

---

## 0. 通过/失败汇总

| 组 | 检查项 | 结果 |
|---|---|---|
| §12.3 PostgreSQL + pgvector | 6 | **6 通过**（1 项附带异常，见 1.5） |
| §12.4 百炼 | 4 | **4 通过**（1 项结论为"上限低于预期"，见 2.1） |
| §12.1 Go | 3 | **3 通过**（E2 风险关闭，见 3.2） |
| §12.2 Python | 3 | **3 通过**（venv 路径与计划书记录不符，已更正） |
| §12.5 跨服务契约 | 5 | **5 通过** |
| **合计** | **21** | **21 通过，0 失败** |

触碰冻结文档的 3 处修订，均依据"唯一例外条款：仅 SPIKE 实测结果可触发"，已在 PRD §14 记 v1.6 并标注 `来源=实测`。

---

## 1. §12.3 PostgreSQL + pgvector

### 1.1 独立容器与扩展

| 项 | 内容 |
|---|---|
| 命令 | `docker run -d --name opspilot-pg-spike -e POSTGRES_PASSWORD=*** -e POSTGRES_DB=spike -p 55433:5432 pgvector/pgvector:pg16`<br>`docker exec opspilot-pg-spike psql -U postgres -d spike -c "CREATE EXTENSION IF NOT EXISTS vector"` |
| 预期 | 容器就绪；扩展创建成功；能读出扩展版本（此前 HNSW 参数未知） |
| 实际 | 容器 PID `364e0d79…`，**1s 就绪**，宿主 `0.0.0.0:55433->5432`；`CREATE EXTENSION` 成功；`extversion = 0.8.6`；PostgreSQL `16.14 (Debian)` |
| 结论 | **通过**。pgvector 0.8.6 支持 HNSW 与 partial index（后续用例验证）；未触碰 55432 的 `pg-local` |

### 1.2 vector(1024) 建表与余弦排序

| 项 | 内容 |
|---|---|
| 命令 | 建 `spike_chunks(id, visible, embedding vector(1024))`，插入 4 条：id1=e1、id2=e2、id3=e1+e2、id4=e1（`visible=false`）；`SELECT id, 1-(embedding <=> q) … WHERE visible ORDER BY … LIMIT 5` |
| 预期 | 维度 1024 校验通过；排序 id1 > id3 > id2；id4 因不可见被排除 |
| 实际 | 四行 `vector_dims = 1024`；返回 `id1=1.0000, id3=0.7071, id2=0.0000`，**id4 未出现** |
| 结论 | **通过**。余弦距离方向正确（cos 越大越近），`visible` 过滤生效 |

### 1.3 partial HNSW 索引

| 项 | 内容 |
|---|---|
| 命令 | `CREATE INDEX idx_spike_visible_hnsw ON spike_chunks USING hnsw (embedding vector_cosine_ops) WHERE visible = TRUE;` 再灌 3000 条随机 1024 维向量 + `ANALYZE`，`EXPLAIN (ANALYZE)` |
| 预期 | 索引创建成功；计划中出现 Index Scan |
| 实际 | `CREATE INDEX` 成功；`pg_indexes` 中 `indexdef` 含 `WHERE (visible = true)`；计划为 `Index Scan using idx_spike_visible_hnsw … actual rows=5` |
| 结论 | **通过**。partial HNSW 可用，且查询带 `WHERE visible=TRUE` 时被选用 |

### 1.4 发布翻转（本协议的关键形态）

| 项 | 内容 |
|---|---|
| 命令 | 翻转前查 top-5 → `UPDATE spike_chunks SET visible=TRUE WHERE id=4` → 再查 top-5 → `EXPLAIN` |
| 预期 | 翻转前 id4 不命中；翻转后 id4 **立即**可检索，无需重建索引 |
| 实际 | 翻转前 `visible=TRUE AND id=4` 计数 **0**；翻转后 top-5 出现 `id=4, cosine=1.0000`（`ef_search=200`）；仍走 `Index Scan` |
| 结论 | **通过（附条件）**：协议成立——不可见行不入索引，UPDATE 后行进入索引、无需重建。但**默认 `ef_search` 下会漏检**，见 1.5 |

### 1.5 异常：HNSW 默认 `ef_search` 召回失败

| 项 | 内容 |
|---|---|
| 命令 | 同一条查询，在 `SET hnsw.ef_search = 40 / 100 / 200` 下取 top-5；并对比"强制全表扫描"与"`REINDEX` 后 ef=40" |
| 预期 | 三种设置都应返回同一组最近邻（与查询向量同向的 id1、id4 应排在最前） |
| 实际 | 全表扫描：`4,1,3,1002,1001`（正确）。<br>`ef=40`：`1219,1218,1217,1216,1215`（**错的，且 cos 仅 0.0041**）<br>`ef=100`：`1361,1360,…`（仍错）<br>`ef=200`：`1,4,3,1003,1004`（正确）<br>`REINDEX` 后 `ef=40` 仍错 |
| 结论 | **召回对 `ef_search` 高度敏感，默认值 40 不可用**。数据分布极端（3 个稀疏单位向量 + 3000 个随机稠密向量，两两距离几乎无区分度）放大了问题，但结论不可回避：**近似索引的召回必须显式配置并实测**。`REINDEX` 无效，说明不是图退化，而是搜索广度不足 |
| 触发修订 | `data-model.md` §2.4 增加查询期 `hnsw.ef_search`（默认 100）、S4 增加"HNSW top-5 vs 精确 top-5 的 Recall@5 ≥ 0.95"；`.env` 增加 `HNSW_EF_SEARCH=100`；PRD §14 v1.6 记 S1-A2 |

### 1.6 迁移 0001 幂等性

| 项 | 内容 |
|---|---|
| 命令 | 在 `mig_test` schema 连续执行两次同段 DDL（`CREATE EXTENSION IF NOT EXISTS` + `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`）；再用 `ALTER TABLE j ADD COLUMN run_token text` 连执行两次 |
| 预期 | 幂等写法可重复执行；`ADD COLUMN` 重复应报错（反证单迁移不可重跑） |
| 实际 | 两次执行均成功（第二次输出 `NOTICE … skipping`）；`ALTER TABLE` 第二次报 `ERROR: column "run_token" relation "j" already exists` |
| 结论 | **通过**。0001 必须以 `IF NOT EXISTS` 写法提供；也印证"0001/0002 拆两份同批上线会 `duplicate column`"的判断（F6 已合并为单 0001） |

---

## 2. §12.4 百炼剩余项

### 2.1 并发与 429 完整形态

| 项 | 内容 |
|---|---|
| 命令 | `text-embedding-v4`，每请求 10 条文本；线程池并发 20 → 再并发 60；记录状态码、响应头、响应体 |
| 预期 | 找到触发限流的并发度，抓取 429 的状态码/headers/body |
| 实际 | **并发 20**：20/20 成功，耗时 2.84–3.58s。<br>**并发 60**：30 成功 / **30 × 429**，耗时 0.41–3.47s。<br>429 响应：`HTTP 429`，`body={"error":{"message":"You have exceeded your current request limit…","type":"limit_requests","code":"limit_requests"},"request_id":"…"}`；**响应头无 `Retry-After`**，含 `x-request-id`、`content-type`、`date` |
| 结论 | **通过**。该账号在此模型上的并发上限≈30；**服务端不给 `Retry-After`**，退避必须自定义 |
| 触发修订 | 计划书 §7.3 补"429 无 `Retry-After`，退避用自定义 1s/2s/4s"；PRD §14 v1.6 记 S1-A3 |

### 2.2 单条输入上限

| 项 | 内容 |
|---|---|
| 命令 | 单个 input 依次 2000 / 8000 / 32000 / 64000 字符（中文），调 `/embeddings` |
| 预期 | 找到硬上限与错误文案，用于标定切片上限 |
| 实际 | 2000 → 200（0.7s）；8000 → 200（**22.7s，异常慢**）；32000 → 200（1.3s）；64000 → **400**，`Range of input length should be [1, 33000]` |
| 结论 | **通过**。单条硬限 **33000 字符**。切片目标 400–600 字符远低于上限，但**单段落超长时必须硬切**，否则整篇失败 |
| 备注 | 8000 字符耗时 22.7s 属偶发（同批其他请求 <2s），说明**延迟方差大**，超时阈值不能按平均值定 |
| 触发修订 | PRD FR-11 增加"单段落 > 33000 字符必须硬切分"；PRD §14 v1.6 记 S1-A1 |

### 2.3 输出上限（max_tokens）

| 项 | 内容 |
|---|---|
| 命令 | `qwen-flash`，`max_tokens` 依次 8192 / 20000 / 400000 |
| 预期 | 确认可接受范围，避免运行期被静默截断 |
| 实际 | 8192 → 200；20000 → 200；400000 → **400**，`Range of max_tokens should be [1, 32768]` |
| 结论 | **通过**。输出上限 **32768**。本项目各节点 `max_tokens ≤ 256`，不受影响；但**参数越界是 400 而非截断**，客户端须预先校验 |

### 2.4 超时与断连

| 项 | 内容 |
|---|---|
| 命令 | ① 输入 ~126k 字符 + 客户端 `timeout=2s`；② 超时后立即发一个小请求；③ `max_tokens=30000` + 要求长输出，`timeout=8s` |
| 预期 | 客户端应在 2s 抛出超时；后续请求不受影响；确认"客户端超时 ≠ 服务端停止"无法从客户端观测 |
| 实际 | ① `TimeoutError: The read operation timed out`，耗时 **2.37s**，连接关闭；② 立即请求 **200，0.54s**，连接池未中毒；③ 200，3.42s（模型未真的长输出，未构成有效压力） |
| 结论 | **通过**。① 客户端超时可被稳定触发；② 无副作用；③ **无法从客户端证明服务端是否继续执行**——这正是本设计按"服务端可能继续跑"做最坏假设（`run_token` CAS + watchdog）的原因，实测无法证伪该假设 |

---

## 3. §12.1 Go 骨架

### 3.1 初始化与构建

| 项 | 内容 |
|---|---|
| 命令 | `go mod init opspilot/backend`；`cmd/api/main.go`（gin，仅 `/health/live`）；`go vet ./...`；`go build -o /tmp/ops-api ./cmd/api` |
| 预期 | 模块初始化、依赖拉取、静态检查、构建均通过 |
| 实际 | `go.mod` 生成；`go mod tidy` 2.37s 成功，gin `v1.12.0`；`go.sum` 89 行；`go vet` 无输出；`build ok` |
| 结论 | **通过** |

### 3.2 GOSUMDB 处置（E2 风险落地）

| 项 | 内容 |
|---|---|
| 命令 | `go env GOPROXY GOSUMDB GONOSUMDB GOFLAGS` → `time go mod tidy` |
| 预期 | 因 `sum.golang.org` 直连不通，可能出现 checksum 校验失败，需要改配置 |
| 实际 | `GOPROXY=https://goproxy.cn,direct`、`GOSUMDB=sum.golang.org`、`GONOSUMDB` 空、`GOFLAGS` 空；**`go mod tidy` 2.37s 成功，89 行 `go.sum` 全部写入** |
| 结论 | **通过，E2 风险关闭**：`goproxy.cn` 代理了 sumdb 查询，无需设置 `GONOSUMDB`/`GOSUMDB=off`。**写死结论：不改任何 Go 环境变量**；若将来换代理导致失败，再把 `GOSUMDB=off` 作为降级手段并记录 |

### 3.3 health 端点

| 项 | 内容 |
|---|---|
| 命令 | `APP_PORT=18081 ./ops-api` 后 `curl /health/live`、`curl /nope` |
| 预期 | 200 + JSON；未定义路由 404 |
| 实际 | `http=200 time=0.0041s`，`{"service":"ops-pilot-backend","status":"ok",…}`；`/nope` → 404 |
| 结论 | **通过** |

---

## 4. §12.2 Python 骨架

### 4.1 venv

| 项 | 内容 |
|---|---|
| 命令 | `"C:/Users/17380/AppData/Local/Programs/Python/Python311/python.exe" -m venv ai-service/.venv` |
| 预期 | 用 3.11 建 venv，与容器 `python:3.11-slim` 对齐 |
| 实际 | 计划书记录的 managed 路径 `…\binaries\python\versions\3.11.9\python.exe` **不存在**（该目录只有 3.13.12）；改用系统 `Python311`，`python --version` = **3.11.9** |
| 结论 | **通过，但计划书事实有误**：已更正 §1.3 的解释器路径（标注"来源=实测"） |

### 4.2 依赖安装（国内源）

| 项 | 内容 |
|---|---|
| 命令 | venv 内写 `pip.ini`（清华源）→ `pip install fastapi uvicorn langgraph "psycopg[binary]" pgvector httpx openai pytest` |
| 预期 | 全部装上，版本可查 |
| 实际 | 耗时 **1m28s**；`fastapi 0.159.1`、`starlette 1.9.0`、`uvicorn 0.54.0`、`langgraph 1.2.16`、`langchain-core 1.6.13`、`psycopg 3.3.6`、`pgvector 0.5.1`、`httpx 0.28.1`、`openai 3.36.1`、`pytest 9.1.1` |
| 结论 | **通过**。注意 `langgraph 1.x` 与文档中"LangGraph"用法需在 S3 按 1.x API 编写（旧 0.x 教程的 `StateGraph` 用法基本兼容，但需实测） |

### 4.3 pytest

| 项 | 内容 |
|---|---|
| 命令 | `cd ai-service && .venv/Scripts/python.exe -m pytest -q` |
| 预期 | 5 条 health/鉴权用例全绿 |
| 实际 | `5 passed in 0.51s` |
| 结论 | **通过** |

### 4.4 uvicorn health

| 项 | 内容 |
|---|---|
| 命令 | `AI_SERVICE_TOKEN=*** uvicorn app.main:app --port 18000`；`curl /health/live`、`curl -X POST /internal/v1/echo` |
| 预期 | health 200；无令牌 401；带令牌中文原样返回 |
| 实际 | `/health/live` → 200；无令牌 echo → **401**；带令牌 echo → 200，`echo_text="网络不通如何排查"`、`text_len=8`、`request_id` 按入参透传 |
| 结论 | **通过** |

---

## 5. §12.5 Go → Python 契约

`go test ./test/ -v`（`SPIKE_PY_URL=http://127.0.0.1:18000`）

| # | 用例 | 预期 | 实际 | 结论 |
|---|---|---|---|---|
| C-01 | 中文往返 | 中文不被破坏，`text_len` 按字符计 | `echo_text` 完全一致，`text_len=8`，`request_id=req-from-go` 透传 | **通过** |
| C-02 | 错误码透传 | Python 的 422 + 错误体原样到达 | `422`，`error.code=INVALID_INGEST_REQUEST` 可解析 | **通过** |
| C-03 | 超时透传 | 客户端 2s 先于服务端 3s 触发 | 耗时 **2.00s** 报错（用例已加"<1.5s 视为连接失败、判定无效"的护栏，防止假通过） | **通过** |
| C-04 | 令牌三态 + health 豁免 | 缺失/错误→401，正确→200；`/health/*` 免鉴权 | 全部符合 | **通过** |
| C-05 | Python 不可达 | Go 能识别为连接错误（503 映射属 S3 业务层） | `*url.Error`（connection refused），进程未崩 | **通过（部分）**。S3 需把该错误映射为 `503 MODEL_UNAVAILABLE`，本轮无业务代码，不实现 |

首轮曾出现 3 条失败，原因是后台 uvicorn 随 shell 退出被终止（`(... &)` 方式不可靠），改用常驻后台任务后全部通过——**S3 的集成测试启动方式应避免同类问题**。

---

## 6. 触发回写的设计修订（唯一例外条款）

| 编号 | 实测结论 | 修订位置 | 是否改需求 |
|---|---|---|---|
| S1-A1 | embedding 单条输入硬限 33000 字符 | PRD FR-11 | 否，收紧实现约束 |
| S1-A2 | HNSW 默认 `ef_search=40` 召回失败，`REINDEX` 无效 | `data-model.md` §2.4、`.env(.example)`、PRD §14 v1.6 | 否，补查询期参数与验收项 |
| S1-A3 | 429 无 `Retry-After` | 计划书 §7.3 | 否，明确退避自定义 |
| S1-A4 | Python 3.11 的 managed 路径不存在 | 计划书 §1.3 | 否，事实更正 |

---

## 7. 未解决风险（进 S3/S4 跟踪）

| # | 风险 | 影响 | 处置 |
|---|---|---|---|
| R1 | HNSW 召回只在合成数据上验证过，真实语料未知 | `visible` 翻转后可能"发布了但检索不到" | S4 用真实文档做 HNSW vs 精确扫描的 Recall@5 ≥ 0.95 |
| R2 | `ef_search=100` 是拍的，无真实语料依据 | 召回或延迟不达标 | S4 扫 40/100/200 找拐点 |
| R3 | 百炼并发上限≈30（实测），入库串行时无影响，并发派发会 429 | 多文档同时摄取易失败 | 摄取默认串行 + 分批 ≤10 + 退避；并发度配置化 |
| R4 | 延迟方差大（同一接口 0.7s vs 22.7s） | P95 目标可能达不到 | 超时阈值按 P95 而非均值定；S8 前实测 |
| R5 | `langgraph 1.2.16` 与示例教程版本差异 | S6 编排可能踩 API 变更 | S6 开工前先跑官方 1.x 最小示例 |

---

## 8. 复现方式

```bash
# 1. pgvector（独立容器，勿占 55432）
docker run -d --name opspilot-pg-spike -e POSTGRES_PASSWORD=*** -e POSTGRES_DB=spike \
  -p 55433:5432 pgvector/pgvector:pg16
docker exec opspilot-pg-spike psql -U postgres -d spike -c "CREATE EXTENSION IF NOT EXISTS vector"

# 2. Go
cd backend && go mod tidy && go build -o /tmp/ops-api ./cmd/api
APP_PORT=18081 /tmp/ops-api

# 3. Python
cd ai-service && .venv/Scripts/python.exe -m pytest -q
AI_SERVICE_TOKEN=*** .venv/Scripts/python.exe -m uvicorn app.main:app --port 18000

# 4. 契约
cd backend && SPIKE_PY_URL=http://127.0.0.1:18000 SPIKE_PY_TOKEN=*** go test ./test/ -v
```

清理：`docker rm -f opspilot-pg-spike`（S1 结束后执行，避免长期占用 55433）。
