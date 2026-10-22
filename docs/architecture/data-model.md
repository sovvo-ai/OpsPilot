# 数据模型（V1）

| 项 | 值 |
|---|---|
| 版本 | v1.1 |
| 状态 | **已冻结（2026-10-22 评审通过）。仅允许 S1 SPIKE 实测结果触发修改；纯措辞/结构性打磨一律拒绝** |
| 上游 | `docs/architecture/ingest-consistency.md` v1.1、`docs/prd/PRD.md` v1.4 §5.5 |
| 数据库 | PostgreSQL 16 + pgvector（容器 `pgvector/pgvector:pg16`，宿主端口 55432） |

> 本文件只覆盖**摄取相关**的完整定义。会话/消息/轨迹表给出字段清单，DDL 细节在 S3 实现时补齐。

---

## 1. 表清单与写者

| 表 | 写者 | 说明 |
|---|---|---|
| `documents` | **Go** | 文档元数据 + 归属 + 去重指纹 |
| `document_contents` | **Go** | 规范化正文（1:1） |
| `ingest_jobs` | **Go 独占** | 任务状态、执行世代、发布标记 |
| `doc_chunks` | **Python 写内容，Go 发布/清理** | 切片 + 向量 + 可见性 |
| `sessions` / `messages` | Go | 会话与消息（引用为 JSONB 快照） |
| `agent_runs` / `tool_calls` | Go | 执行轨迹（Python 通过响应回传） |

> `doc_chunks` 的双方写入是**有约束的例外**：Python 只能 `INSERT ... visible=false` / `DELETE WHERE doc_id=$1`；Go 只能 `UPDATE visible=true WHERE run_token=$t` / `DELETE WHERE run_token<>$t`。两侧都不允许改对方负责的字段（内容 vs 可见性）。

---

## 2. 摄取相关表 DDL

### 2.1 documents

```sql
CREATE TABLE documents (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id     TEXT        NOT NULL,                 -- JWT sub，V1 为白名单身份
  title        TEXT        NOT NULL,                 -- 文件名（NFC 规范化）
  source_hash  CHAR(64)    NOT NULL,                 -- sha256(规范化全文)，仅用于本人去重
  byte_size    INTEGER     NOT NULL,
  char_count   INTEGER     NOT NULL,
  deleted_at   TIMESTAMPTZ,                          -- 软删标记（deleting 期间非空）
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 去重范围限定在单个 owner 内；不得建全局唯一索引（决策 Q6）
CREATE UNIQUE INDEX uq_documents_owner_hash
  ON documents (owner_id, source_hash)
  WHERE deleted_at IS NULL;

CREATE INDEX idx_documents_owner_created
  ON documents (owner_id, created_at DESC);
```

**关于 `source_hash` 的隐私约定（决策 Q6）**：

| 项 | 规定 |
|---|---|
| 用途 | 仅"同一用户重复上传去重" |
| 隔离范围 | 索引键含 `owner_id`，**不跨用户比对**；不存在全局指纹表 |
| 访问控制 | **不出现在任何 API 响应中**；不写日志（日志只记 `doc_id`） |
| 保留规则 | 随文档行删除而消失；无独立留存 |
| 明确不宣称 | 不声称"SHA-256 后无隐私风险"。它仍是内容指纹，只是**不在用户之间做关联** |

### 2.2 document_contents

```sql
CREATE TABLE document_contents (
  doc_id         UUID PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
  content        TEXT        NOT NULL,        -- NFC + 统一 \n + 去 BOM
  char_count     INTEGER     NOT NULL,
  token_estimate INTEGER     NOT NULL,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 2.3 ingest_jobs

```sql
CREATE TABLE ingest_jobs (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  doc_id              UUID NOT NULL UNIQUE REFERENCES documents(id) ON DELETE CASCADE,
  status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','processing','completed','failed','deleting')),
  attempt             INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),  -- 已执行次数；0=尚未执行，抢占 +1 后即"第几次执行"
  current_run_token   TEXT,                 -- 当前执行实例标识（32 hex）
  published_run_token TEXT,                 -- 已发布到检索的世代
  chunk_count         INTEGER NOT NULL DEFAULT 0,
  error_code          TEXT,                 -- 终态错误码，见 §4
  error_msg           TEXT,
  started_at          TIMESTAMPTZ,
  finished_at         TIMESTAMPTZ,
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 每次状态变更必须 SET now()，watchdog 收龄依赖它
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- watchdog 扫描：pending 用 updated_at（reingest 会刷新），processing 用 started_at
CREATE INDEX idx_ingest_jobs_watchdog
  ON ingest_jobs (status, COALESCE(started_at, updated_at));
```

`doc_id` 上的 `UNIQUE` 保证"一文档一任务"，从根上避免重复任务行。

### 2.4 doc_chunks

```sql
CREATE TABLE doc_chunks (
  chunk_id     UUID PRIMARY KEY,                  -- uuidv5(doc_id + chunk_index)
  doc_id       UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  run_token    TEXT,                              -- 产出该切片的执行实例
  visible      BOOLEAN NOT NULL DEFAULT FALSE,    -- 发布闸门；检索只认 TRUE
  chunk_index  INTEGER NOT NULL,
  heading_path TEXT,
  char_start   INTEGER NOT NULL,
  char_end     INTEGER NOT NULL,
  content      TEXT NOT NULL,
  embedding    vector(1024) NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 检索主索引：只在可见切片上建 HNSW
CREATE INDEX idx_doc_chunks_embedding
  ON doc_chunks USING hnsw (embedding vector_cosine_ops)
  WHERE visible = TRUE;

CREATE INDEX idx_doc_chunks_doc_visible ON doc_chunks (doc_id, visible);
CREATE INDEX idx_doc_chunks_run_token   ON doc_chunks (run_token);
```

**查询期必须显式设置 `hnsw.ef_search`（S1 实测，v1.1 增补）**：

| 项 | 值 | 依据 |
|---|---|---|
| 默认值 | **100**（由 `HNSW_EF_SEARCH` 配置） | pgvector 默认 40 在 3000 条 1024 维合成向量上**未能召回与查询向量同向的切片**（cosine=1.0）；`ef_search=200` 正确命中，`REINDEX` 无法修复 |
| 校验要求 | S4 验收增加"HNSW top-5 与精确全表扫描 top-5 的 Recall@5 ≥ 0.95" | 近似索引召回必须实测，不能假设 |
| 说明 | 该实测数据分布极端（稀疏单位向量 + 随机稠密向量，距离几乎无区分度），真实 embedding 空间可能更友好；但这正说明**必须用真实语料复测**，不能用合成数据下结论 | — |

**检索 SQL 必须带可见性与状态双重过滤**（缺一即为缺陷）：

```sql
SELECT c.chunk_id, c.doc_id, c.content, 1 - (c.embedding <=> $1) AS score
FROM doc_chunks c
JOIN ingest_jobs j ON j.doc_id = c.doc_id
JOIN documents d   ON d.id = c.doc_id
WHERE c.visible = TRUE
  AND j.status = 'completed'
  AND d.owner_id = $2
  AND d.deleted_at IS NULL
ORDER BY c.embedding <=> $1
LIMIT $3;
```

### 2.5 其余表（字段清单，DDL 在 S3 补齐）

| 表 | 关键字段 |
|---|---|
| `sessions` | `id, owner_id, title, created_at, updated_at` |
| `messages` | `id, session_id, role, content, citations JSONB, degraded BOOL, run_id, created_at` |
| `agent_runs` | `id, session_id, message_id, owner_id, route, llm_calls, tool_calls, total_tokens, degraded, terminated_reason, duration_ms, created_at` |
| `tool_calls` | `id, run_id, step_index, tool_name, args_digest, status, duration_ms, created_at` |

---

## 3. 迁移

### 3.1 迁移清单

| 编号 | 文件 | 内容 | 阶段 |
|---|---|---|---|
| **0001** | `0001_init.up.sql` / `.down.sql` | `CREATE EXTENSION vector` + **全部表**（含 `run_token` / `attempt` / `visible` / `deleting` / `(owner_id, source_hash)` 索引）+ 全部索引 | S3 |

> **v1.0 曾拆成 0001 + 0002，是错的**：项目零部署，0001 若按 §2 终态建表，0002 再 `ADD COLUMN current_run_token` 必然报 `column already exists`。两份迁移放在同批上线等于自己打自己。现合并为单个 0001。
>
> **"哪些字段是为异步一致性加的"这个信息不丢失**：见 §3.2 的字段清单，它保留说明作用，但不再是一份独立迁移。

### 3.2 为异步一致性引入的字段（说明用，不是独立迁移）

| 表 | 字段 | 作用 |
|---|---|---|
| `ingest_jobs` | `current_run_token` | 当前执行实例标识，发布时 CAS 校验 |
| `ingest_jobs` | `published_run_token` | 已发布世代，用于追溯"哪次执行产出了当前可见切片" |
| `ingest_jobs` | `attempt` | 已执行次数：建行 0（未执行），每次抢占 +1；首次执行后为 1 |
| `ingest_jobs` | `started_at` / `finished_at` / `updated_at` | 超时判定与收龄 |
| `ingest_jobs` | `chunk_count` | 发布时写入，用于零切片闸门与展示 |
| `ingest_jobs` | `error_code` / `error_msg` | 终态原因（**没有 `last_error` 这个列**） |
| `doc_chunks` | `run_token` | 该切片由哪次执行产出 |
| `doc_chunks` | `visible` | 发布闸门，检索只认 `TRUE` |
| `documents` | `deleted_at` | 软删标记，`deleting` 期间非空 |
| `documents` | `(owner_id, source_hash)` 联合唯一索引 | 去重限定在单用户内 |

### 3.3 数据回填

**不适用**：项目尚未在任何环境部署过，不存在需要回填的历史数据。

若在 S3 之前已有本地手工试验数据，按以下顺序处理（幂等）：

```sql
UPDATE doc_chunks SET visible = TRUE
 WHERE doc_id IN (SELECT doc_id FROM ingest_jobs WHERE status = 'completed');
UPDATE ingest_jobs SET published_run_token = current_run_token WHERE status = 'completed';
UPDATE ingest_jobs SET status='failed', error_code='DISPATCH_FAILED',
       finished_at=now(), updated_at=now()
 WHERE status IN ('pending','processing');
```

### 3.4 兼容性与回滚

| 维度 | 说明 |
|---|---|
| **前向兼容** | 新增列全部可空或有默认值，旧代码 INSERT 不会失败 |
| **读兼容风险（重要）** | 旧版检索 SQL 不过滤 `visible`，会看到未发布切片 → **迁移必须与代码同版本发布**，不允许"先迁库后发代码"或反之 |
| **回滚** | `.down.sql` 删列；但若已有 `visible=false` 数据，回滚前必须先删除这些行，否则回滚后它们会被当成正常切片。**回滚脚本第一段就是清理孤儿** |
| **索引重建成本** | HNSW 索引在空表上建立，成本可忽略；若已有数据，重建 1024 维 HNSW 需数十秒 |
| **锁影响** | `ALTER TABLE ... ADD COLUMN` 带默认值在 PG 11+ 是元数据操作，不重写表；`DROP/ADD CONSTRAINT` 会短暂取排他锁，V1 单实例无影响 |

---

## 4. 任务级错误码

| code | 触发 | 是否可 retry | 用户文案 |
|---|---|---|---|
| `DISPATCH_FAILED` | `pending` 超 120s 未被抢占 | 是 | 任务未能启动，请重试 |
| `TIMEOUT` | `processing` 超 150s | 是 | 处理超时，请重试 |
| `PARSE_ERROR` | 解析失败（Python 返回） | 否（需修文件） | 文件解析失败，请检查内容 |
| `EMPTY_CONTENT` | 解码后为空 | 否 | 文件内容为空 |
| `EMBEDDING_FAILED` | Embedding 调用失败/次数耗尽 | 是 | 向量化失败，请重试 |
| `RATE_LIMITED` | 429 退避耗尽 | 是 | 服务繁忙，请稍后重试 |
| `DB_ERROR` | 写库失败 | 是 | 服务异常，请稍后重试 |

> 这些是**任务层**码，出现在 `GET /documents/{id}` 的 `error_code` 字段；HTTP 层错误码见 `docs/api/ingest-contract.md`。两层不混用。

---

## 5. 修订记录

### v1.1（2026-10-22 19:40）

| # | 问题 | 修复 |
|---|---|---|
| F3 | 无 `last_error` 列（其他文档 SQL 里出现过） | 明确只有 `error_code` / `error_msg`，并在 §3.2 标注"没有 last_error 这个列" |
| F5 | `updated_at` 有默认值但从不更新 | 列注释写明"每次状态变更必须 SET now()"；watchdog 索引改 `(status, COALESCE(started_at, updated_at))` |
| F6 | 0001 与 0002 同时上线会 `duplicate column` | **合并为单个 0001**；"一致性相关字段"改为 §3.2 的说明表，不再是独立迁移 |
**同轮追加修复（19:43）**

| # | 问题 | 修复 |
|---|---|---|
| G2 | 头部版本仍 v1.0，上游引用 consistency v1.0 | 改 v1.1 / v1.1 / PRD v1.4 |
| G3 | `attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1)` 导致首次执行记成 2 | 改 `DEFAULT 0`、`CHECK (attempt >= 0)`，语义为"已执行次数" |
