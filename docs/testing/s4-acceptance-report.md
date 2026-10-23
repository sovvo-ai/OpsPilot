# S4 摄取链路验收报告

| 项 | 值 |
|---|---|
| 日期 | 2026-10-23 12:14 – 13:05（GMT+8） |
| 执行人 | 开发侧（AI 辅助） |
| 依据 | `docs/prd/PRD.md` §11.1（A1–A15）、`docs/testing/test-matrix.md`（T-01~T-09）、`docs/architecture/ingest-consistency.md` §4 |
| 结论 | **S4 通过**：13/13 验收项全绿，Go/Python 质量门禁全绿 |
| 执行方式 | `docker compose build --no-cache` → `up -d` → 宿主机运行真实 HTTP + 真实 DB 的验收脚本 |
| 环境 | Windows + Docker Desktop 28.5.1；PostgreSQL 16 + pgvector 0.8.6；DashScope `text-embedding-v4`(dim=1024) |

> **脱敏声明**：`DASHSCOPE_API_KEY`、`AI_SERVICE_TOKEN`、`JWT_SECRET`、`POSTGRES_PASSWORD` 一律从 `deploy/.env` 读取并打码，从未打印。

---

## 0. 通过/失败汇总

| 组 | 项数 | 结果 |
|---|---|---|
| A1 摄取成功与延迟 | 2 | **2 通过**（5 篇 completed、P95 4.37s ≤ 30s） |
| A13 身份与资源隔离 | 1 | **1 通过**（4 项交叉访问均 404） |
| A14 状态机不变量 | 1 | **1 通过**（6 条 SQL 断言全 0） |
| A15 删除彻底性 | 1 | **1 通过**（四表残留全 0） |
| 去重 | 1 | **1 通过**（deduplicated 且 chunk 数未增） |
| failed → reingest | 1 | **1 通过**（切片不重复） |
| T-02 迟到结果丢弃 | 1 | **1 通过** |
| T-03 迟到 run vs 新 run | 1 | **1 通过** |
| T-07 超时阈值边界 | 1 | **1 通过**（149s 不收敛 / 151s 收敛） |
| T-08 零切片不发布 | 1 | **1 通过**（两段断言） |
| T-09 chunk_count 一致性 | 1 | **1 通过** |
| HNSW Recall@5 | 1 | **1 通过**（ef 40/100/200 均 1.00） |
| **合计** | **13** | **13 通过，0 失败** |

质量门禁：`go vet ./...` 干净；`go test ./...` 全绿；`pytest` 三种环境全绿（30 passed / 26 passed+4 skipped）。

---

## 1. 场景清理与调试输出改造

### 1.1 命令

```bash
# 根目录临时 md 与调试脚本清理
rm -f test_services.py test_simple.py scripts/debug_ingest.py
rm -f S4_实现总结.md S4_验收结果总结.md   # 上一轮遗留临时产物
```

### 1.2 调试输出改 logging

`[ingest debug]` / `[embed debug]` 均替换为 `logging`（`ai-service/app/ingest.py`）；
`[dispatcher debug]` 收敛为结构化 `log.Printf("ingest done doc_id=%s elapsed=%.1fs status=%s chunks=%d" ...)`（`backend/internal/ingest/dispatcher.go`）。
`ai-service/app/main.py` 增加 `logging.basicConfig`，等级取 `LOG_LEVEL`。

---

## 2. chunker 修复：碎片合并

### 2.1 问题

原实现用固定 80 字符回退边界，导致**每篇文档末尾都产生 `[80, 极小值]` 两个碎片**。

### 2.2 修复

`chunk_text` 重写为段落级聚合：`_split_paragraphs`（代码围栏内不切段）→ 均衡打包（按 `TARGET_CHUNK_SIZE=500` 估算块数，均分段落）→ 尾部重平衡（`MIN_CHUNK_SIZE=200` 以下并入相邻块）→ 硬限保护（`HARD_CUT_SIZE=33000`）。

### 2.3 命令与输出

```bash
ai-service/.venv/Scripts/python.exe -m pytest tests/test_chunker.py -q
```

```
k8s-pod-troubleshooting  n=2 sizes=[581, 653] <200=0 cov=100%
linux-network-...        n=1 sizes=[674]      <200=0 cov=100%
mysql-slow-query         n=2 sizes=[451, 465] <200=0 cov=100%
nginx-502-upstream       n=2 sizes=[392, 444] <200=0 cov=100%
observability-logging    n=1 sizes=[447]      <200=0 cov=100%
总体: n=8  300~700 覆盖率=100%
18 passed in 0.56s
```

**结论**：FR-11 分布覆盖率 100%（门槛 ≥90%），碎片数为 0，5 篇种子文档全覆盖。

---

## 3. 镜像重建与启动

### 3.1 命令

```bash
cd deploy
docker compose build --no-cache
docker compose up -d --force-recreate
```

### 3.2 输出片段

```
 deploy-ai-service  Built
 deploy-backend  Built
 Container opspilot-postgres  Healthy
 Container opspilot-ai  Started
 Container opspilot-backend  Started
```

```bash
docker ps --filter name=opspilot --format 'table {{.Names}}\t{{.Status}}'
```

```
NAMES               STATUS
opspilot-ai         Up 6 minutes
opspilot-backend    Up 15 minutes
opspilot-postgres   Up 26 minutes (healthy)
```

```bash
curl -s http://127.0.0.1:8081/health/ready
```

```json
{"status":"ok","healthy":true,"checks":{"ai_service":{"status":"ok"},"database":{"status":"ok"},"migrations":{"status":"ok","detail":"0001_init"}}}
```

---

## 4. 验收 12 项实测（命令 + 输出）

### 4.1 运行命令

```bash
# 前置：完整栈已启动（见 §3）
cd deploy && docker compose up -d --force-recreate && cd ..

# 执行验收（脚本在宿主机运行，直连 compose 映射端口）
export POSTGRES_HOST=localhost BACKEND_URL=http://127.0.0.1:8081 AI_SERVICE_URL=http://127.0.0.1:8000
ai-service/.venv/Scripts/python.exe scripts/s4_acceptance.py
```

> 说明：compose 为 ai-service 映射了宿主 `8000` 端口（`deploy/docker-compose.yml`），
> 因为验收脚本需要从宿主机直连 `/internal/v1/ingest` 以构造 T-02/T-03 的"迟到写入"。
> 生产部署可移除该映射，仅走 compose 内网。

### 4.2 A1：5 篇种子文档 202 → 轮询 completed

```
【A1】上传 5 篇种子文档并轮询到 completed
  ✅ k8s-pod-troubleshooting.md: completed, chunks=2, total_time=2.16s
  ✅ linux-network-troubleshooting.md: completed, chunks=1, total_time=4.30s
  ✅ mysql-slow-query.md: completed, chunks=2, total_time=4.27s
  ✅ nginx-502-upstream.md: completed, chunks=2, total_time=4.34s
  ✅ observability-logging.md: completed, chunks=1, total_time=4.36s

  5 篇摄取耗时: min=2.16s, max=4.36s, P95=4.37s
  ✅ P95=4.37s ≤ 30s
```

### 4.3 A13：身份与资源隔离

```
【A13】身份与资源隔离
  ✅ test-user-2 读 demo-user 文档: HTTP 404
  ✅ test-user-2 reingest demo-user 文档: HTTP 404
  ✅ test-user-2 删除 demo-user 文档: HTTP 404
  ✅ demo-user 读 test-user-2 不存在的文档: HTTP 404
```

### 4.4 去重零 Embedding

```
【A1-dedup】重复上传同一文档，验证零 Embedding 调用
  重复上传 k8s-pod-troubleshooting.md: status=202, deduplicated=True
  重复前 chunks=2, 重复后 chunks=2, 未变=True
  ✅ 零 Embedding 调用（chunk 数量未增加）
```

### 4.5 failed → reingest

```
【failed → reingest】人为置 failed 后手动重试
  初始状态: status=completed, visible_chunks=2
  置 failed 后: status=failed, error_code=MANUAL_FAIL
  reingest 响应: status_code=202, ingest_status=pending
  reingest 前 visible=2, 后 visible=2, 无重复=True
  ✅ failed → reingest 成功且切片不重复
```

### 4.6 T-08 零切片不发布（两段断言）

```
【T-08】零内容/零切片不得进入 completed 流程
  (a) 空白文档上传: status_code=422, code=EMPTY_CONTENT
      ✅ 零内容在上传阶段被拒绝，未落库/未触发 Embedding
  (b) 清空正文后 reingest: status=failed, error_code=EMPTY_CONTENT, visible=0
      ✅ 零切片未发布为 completed
  ✅ T-08 总体
```

### 4.7 T-02 迟到结果被丢弃

```
【T-02】迟到结果被丢弃（stale_result_dropped）
  模拟迟到响应: status_code=200, body_status=ok
  最终状态: status=failed, error_code=TIMEOUT, visible=0
  ✅ 迟到结果未覆盖最终状态
```

### 4.8 T-03 迟到 run vs 新 run

```
【T-03】迟到 run 被丢弃，reingest 新 run 保留
  基线 run 完成: chunks=2
  run B 完成: published_run_token=14f21015bf6c..., chunks=2
  模拟 run A 迟到响应: status_code=200, body_status=ok
  验证: published_run_token 未变=True, 非 run B 的 visible 切片=0
  ✅ run B 结果未被迟到 run A 覆盖
```

### 4.9 T-07 超时阈值边界

```
【T-07】processing 超时阈值边界（阈值 150s）
  149s 不收敛: True（would_reap=False）
  151s 已收敛: True（would_reap=True）
  ✅ 超时阈值边界正确
```

### 4.10 A14 / T-04 / T-05 不变量 SQL 断言

```
【A14 / T-04 / T-05】状态机不变量 SQL 断言
  ✅ i_visible_only_completed: 0
  ✅ i_completed_has_visible: 0
  ✅ i_deleting_has_deleted_at: 0
  ✅ i_run_token_matches_published: 0
  ✅ i_status_enum: 0
  ✅ i_chunk_count_matches_visible: 0
```

### 4.11 T-09 chunk_count 一致性

```
【T-09】发布后 chunk_count 与实际 visible 切片一致
  ✅ b4c82e58-...: chunk_count=1, visible=1
  ✅ a75e0db8-...: chunk_count=2, visible=2
  ✅ 522e2708-...: chunk_count=2, visible=2
  ✅ 92ff1a37-...: chunk_count=1, visible=1
  ✅ 4550fbc0-...: chunk_count=2, visible=2
```

### 4.12 A15 删除链路与删除彻底性

```
【A15】删除链路与删除彻底性
  待删除文档就绪: chunks=2, visible=2
  DELETE 响应: status_code=202, ingest_status=deleting
  deleting 期间: documents 行存在=True, job.status=deleting, deleted_at=已置位
  四表残留计数: {'documents': 0, 'document_contents': 0, 'ingest_jobs': 0, 'doc_chunks': 0}
    ✅ documents: 0
    ✅ document_contents: 0
    ✅ ingest_jobs: 0
    ✅ doc_chunks: 0
  ✅ A15 删除彻底性
```

### 4.13 HNSW Recall@5（真实语料，ef 扫描）

```
【HNSW Recall@5】真实语料召回验证

  ef_search=40
    Pod 反复重启 CrashLoopBa Recall@5=1.00
    Nginx 502 上游连接被重置    Recall@5=1.00
    MySQL 慢查询日志如何开启      Recall@5=1.00
    Linux 服务器网络不通如何排查    Recall@5=1.00
    日志采集与聚合架构            Recall@5=1.00
  ef=40: avg=1.000, min=1.000 >=0.95

  ef_search=100  … avg=1.000, min=1.000 >=0.95
  ef_search=200  … avg=1.000, min=1.000 >=0.95
```

**ef 扫描数字**：ef_search ∈ {40, 100, 200} 三档下 Recall@5 均为 **1.000**（min 亦为 1.000），全部 ≥ 0.95 门槛。

### 4.14 汇总

```
验收结果汇总
  ✅ A1_upload_5_docs       ✅ A1_p95_le_30s
  ✅ A13_isolation          ✅ A1_dedup_zero_embedding
  ✅ failed_reingest        ✅ T08_zero_chunks
  ✅ T02_late_result_dropped ✅ T03_late_vs_new_run
  ✅ T07_timeout_threshold  ✅ A14_invariants
  ✅ T09_chunk_count        ✅ A15_deleting_chain
  ✅ HNSW_recall

总计: 13/13 通过
```

---

## 5. 本轮修复的真实缺陷

### 5.1 容器外呼被宿主代理污染（阻断所有 Embedding）

| 项 | 内容 |
|---|---|
| 现象 | 5 篇摄取全部 `failed(EMBEDDING_FAILED)`，`error_msg=[Errno 111] Connection refused` |
| 根因 | Docker Desktop 把宿主 `http_proxy/https_proxy=127.0.0.1:55254` 注入容器；容器内 `127.0.0.1` 指向容器自身，必然被拒。compose 只清了大写 `HTTP_PROXY`，小写变量仍泄漏 |
| 修复 | `ai-service/app/ingest.py` 新增 `_build_client()`：外呼 `trust_env=False`，默认不继承环境代理，仅当显式配置 `OUTBOUND_PROXY` 时走代理（与 Go 侧 dispatcher 的 `Transport{Proxy: nil}` 对齐）；`deploy/.env` 代理留空 |
| 验证 | 容器内 `embed_texts(['...'])` → `OK dim=1024` |

### 5.2 重摄取主键冲突（T-03 暴露）

| 项 | 内容 |
|---|---|
| 现象 | 重摄取 run B 返回 `status=error chunks=0`，最终 `failed` |
| 根因 | `chunk_id = uuidv5(doc_id + chunk_index)` 是**确定性**的；`ingest_document` 缺少设计文档 §4.1 要求的"阶段一先 `DELETE FROM doc_chunks WHERE doc_id=$doc_id`（清全世代）"，导致新世代 INSERT 与旧世代主键冲突 |
| 修复 | `ingest_document` 在同一事务内先清全世代再写 `visible=false` 切片，完全对齐 `docs/architecture/ingest-consistency.md` §4.1 |
| 回归保护 | 新增 `ai-service/tests/test_ingest_db.py`（4 例，真实 DB）：首次全不可见 / 重摄取清旧世代无主键冲突 / 发布翻可见并删旧世代 / chunk_id 确定性 |

### 5.3 测试脚本自身的构造缺陷

| 项 | 内容 |
|---|---|
| T-07 阈值判定 | `interval '%s seconds'` 的参数化写法被 psycopg 当作字面量，实际不回溯时间，且"状态快照"判定会被常驻 watchdog 抢先收割。改为**原子阈值谓词**（单条 SELECT 内对 `started_at` 求值），与 watchdog 实现 SQL 语义一致 |
| T-03 状态链依赖 | 原用例依赖前序 T-02 的失败状态，脆弱。改为自包含构造（新上传 → 回拨 → 置 failed → reingest 产生 run B） |
| T-08 错误体解析 | 后端错误体是嵌套的 `{"error":{"code":...}}`，脚本取顶层 `code` 取不到。新增 `extract_error_code()` 兼容两种结构 |
| 跨用例 dedup 污染 | 多个用例重复上传同一 seed 文档会命中 dedup 拿到他人文档。改用 `make_unique_doc()` 生成内容唯一的临时文档 |

---

## 6. 质量门禁

```bash
cd backend && go vet ./... && go test ./...
```

```
exit=0
ok  	opspilot/backend/internal/handlers	(cached)
ok  	opspilot/backend/internal/ingest	0.718s
ok  	opspilot/backend/test	(cached)
```

```bash
cd ai-service && pytest -q     # 有 DB + 有 key / 有 DB 无 key / 无 DB 无 key
```

```
30 passed   /   30 passed   /   26 passed, 4 skipped
```

新增测试：`backend/internal/ingest/store_test.go`（11 例，上传校验/哈希/令牌/UUID/编码）、`ai-service/tests/test_chunker.py`（18 例）、`ai-service/tests/test_ingest_db.py`（4 例集成）。

---

## 7. 结论

**S4 摄取链路通过验收，13/13 全绿，可提交。**

- 核心链路（上传 → 校验 → 事务 → 派发 → 切片 → Embedding → 不可见写入 → 两阶段发布 → 清理）全部走通并经真实数据验证。
- 三项真实缺陷（代理污染、重摄取主键冲突、脚本构造缺陷）已定位并修复，其中两项补了回归测试。
- HNSW 真实语料 Recall@5 在 ef 40/100/200 三档下均为 1.000，S1 遗留的 R1/R2 欠账结清。