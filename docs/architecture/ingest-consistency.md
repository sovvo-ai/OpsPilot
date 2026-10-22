# 异步摄取一致性设计（V1）

| 项 | 值 |
|---|---|
| 版本 | v1.1 |
| 状态 | **已冻结（2026-10-22 评审通过）。仅允许 S1 SPIKE 实测结果触发修改；纯措辞/结构性打磨一律拒绝** |
| 上游 | `docs/prd/PRD.md` §5.5–§5.7、Q1–Q6 决策 |
| 范围 | 仅异步摄取的一致性、并发、恢复与删除；不含检索算法与 Agent 编排 |

---

## 0. 一句话结论

**Go 是任务状态的唯一写者，Python 是向量数据的唯一生成者；Python 写出的切片默认不可见（`visible=false`），必须由 Go 用 `run_token` 校验后"发布"才进入检索。** 旧执行永远无法发布过期向量，因为它拿不到当前的 `run_token`。

---

## 1. 职责边界（决策 Q1）

| 关注点 | Go | Python |
|---|---|---|
| 任务状态（`ingest_jobs.status`） | **唯一写者**，含终态收敛 | **只读**（可读 `current_run_token` / `attempt`） |
| 抢占任务、`attempt` 递增、新生 `run_token` | **是** | 否 |
| 解析 / 切片 / Embedding | 否 | **是** |
| 写入切片内容（`doc_chunks` INSERT） | 否 | **是**，写入时 `visible=false`、带 `run_token` |
| 发布切片（`visible=true`）+ 清理旧世代 | **是** | 否 |
| `documents` / `document_contents` | **唯一写者** | 只读 |
| watchdog（收敛超时任务） | **是**（进程内定时器，60s） | 否 |
| 删除流程编排 | **是** | 只按指令清向量 |

**为什么不让 Python 写终态**：一旦双方都能写终态，就必须引入分布式锁或两阶段提交来防覆盖。把终态收归 Go 单方写入，用 `run_token` 做版本校验，等价于乐观并发控制，不需要额外组件。

---

## 2. 状态机

```
                     ┌──────────── watchdog: pending 超 120s ────────────┐
                     │                                                   ▼
  上传落库        抢占(claim)                                      failed(DISPATCH_FAILED)
  ────────► pending ──────────► processing ──── 成功返回 ────► completed
                ▲                  │  │
                │                  │  └──── 失败返回 ──────► failed(EMBEDDING_FAILED / PARSE_ERROR …)
                │                  │
                │                  └──── watchdog: processing 超 150s ──► failed(TIMEOUT)
                │
                └── reingest（仅 failed → pending，attempt 在下次抢占时 +1）

  pending / processing / failed / completed ──delete──► deleting ──清理完成──► (行删除)
```

**状态枚举**：`pending | processing | completed | failed | deleting`（V1 引入 `deleting`，理由见 §6）

| 状态 | 可检索 | 可 reingest | 可删除 | 说明 |
|---|---|---|---|---|
| `pending` | 否 | 否（等派发） | 是 | 已落库，等待 Go 抢占 |
| `processing` | 否 | 否 → 409 `DOC_BUSY` | 是（→ `deleting`） | 有执行者在跑 |
| `completed` | **是** | 否 → 409 `DOC_NOT_RETRYABLE` | 是 | 切片已发布 |
| `failed` | 否 | **是** | 是 | 有终态错误 |
| `deleting` | 否 | 否 → 409 `DOC_DELETING` | 幂等返回 202 | 正在清理，可能残留向量 |

**非法迁移一律拒绝且记审计**：

- `completed → processing` **禁止**：`completed` 不可重试，也不可由抢占触达（抢占只认 `pending`）。
- `failed → processing` **禁止**：必须经 `reingest` 先回 `pending`（见 §3）。
- `deleting → *` **禁止**：只允许"清理完成后删除行"这一条出路。
- `pending → completed` **禁止**：必须先 `processing` 抢占，保证 `attempt` 与 `run_token` 已写入。

---

## 3. 抢占协议（决策 Q3 / Q5）

抢占是一个**单条原子条件更新**，不加显式锁：

```sql
-- Go，一次语句完成：状态、执行标识、attempt、时间戳
UPDATE ingest_jobs
   SET status = 'processing',
       attempt = attempt + 1,
       current_run_token = $2,          -- 新生成的 token
       started_at      = now(),
       finished_at     = NULL,
       error_code      = NULL,
       error_msg       = NULL,
       updated_at      = now()
 WHERE id = $1
   AND status = 'pending'                -- 只从 pending 抢；failed 必须先经 reingest 回到 pending
RETURNING attempt, current_run_token;
```

**为什么只允许 `pending`**：`failed → processing` 绕过 `reingest`，等于任何游离 goroutine 都能复活失败任务，与"V1 不自动重试"（§5.2）正面冲突。重试路径必须是 `failed → pending`（用户显式请求）→ `pending → processing`（抢占），两步分离。

- **影响行数 0 = 抢占失败**（任务已被别的执行者抢走，或状态不允许）。直接放弃，不重试、不报错给用户，记审计 `claim_conflict`。
- `attempt` 语义是**"已执行次数"**：建行默认 **0**（尚未执行），每次成功抢占 **+1**，因此首次执行后为 1、第二次为 2。`reingest` 请求本身不递增，只有真正开始执行才递增。
- 抢占事务提交后，Go 才向 Python 发起调用。**不存在"先调 Python 再改状态"的顺序**，否则会双跑。
- 批量抢占（watchdog 一次处理多条）使用 `FOR UPDATE SKIP LOCKED`；**V1 先只做单条条件更新，批量版本由实测决定是否引入**（决策 Q5 的"如有实际需要"）。

---

## 4. 两阶段发布协议（决策 A）

### 4.1 阶段一：Python 产出，但不可见

Go 调用：

```
POST /internal/v1/ingest
{"job_id":"...","doc_id":"...","run_token":"<32 hex>","attempt":2}
```

Python 在**自己的事务**内完成：

```sql
DELETE FROM doc_chunks WHERE doc_id = $doc_id;              -- 清全世代（含旧世代与孤儿），理由见 §4.4
INSERT INTO doc_chunks (chunk_id, doc_id, run_token, visible, content, embedding, ...)
VALUES (..., $run_token, FALSE, ...), ...;                  -- 关键：visible = FALSE
```

然后返回：

```json
{"run_token":"<同一 token>","status":"ok","chunk_count":37}
```

**Python 不写 `ingest_jobs`**，只返回结果。此时切片存在于库中但检索不可见。

### 4.2 阶段二：Go 校验并发布

**先做零切片闸门（F4）**：`status=ok` 但 `chunk_count = 0` 时**不发布**，直接走失败路径置 `failed(EMPTY_CONTENT)`，并清本次 `visible=false` 切片。

理由：发布 `completed` + 0 个可见切片会直接击穿不变量 I-07/A14②（"completed 必有可见切片"）。极端情况确实存在：切片器对全空白内容产出 0 片、解析器吞掉全部内容、并发竞态下正文被清空。入口拦一次比事后修数据便宜。

`chunk_count = 0` 的判定放在 Go 侧而非 Python 侧：Python 只报事实，是否接受是业务决策，归 Go。

校验通过后，Go 在**自己的事务**内：

```sql
BEGIN;

-- ① 版本校验 + 状态迁移（CAS），这是唯一的发布闸门
UPDATE ingest_jobs
   SET status = 'completed',
       published_run_token = $token,
       finished_at = now(),
       chunk_count = $n,
       updated_at = now()
 WHERE id = $job_id
   AND status = 'processing'
   AND current_run_token = $token;

-- 影响行数 = 0 → ROLLBACK，记审计 stale_result_dropped，终止（不报错给用户）

-- ② 发布本世代切片
UPDATE doc_chunks SET visible = TRUE
 WHERE doc_id = $doc_id AND run_token = $token;

-- ③ 清理旧世代（正常路径下应为 0 行；重试路径会有）
DELETE FROM doc_chunks
 WHERE doc_id = $doc_id AND run_token IS DISTINCT FROM $token;

COMMIT;
```

**为什么这样就杜绝了过期向量**：

1. `visible=false` 的切片**永不参与检索**（检索 SQL 强制 `visible = TRUE`）。
2. 发布必须过 CAS：`current_run_token = $token AND status='processing'`。旧执行的 token 在下次抢占时已被覆盖，CAS 必然 0 行。
3. 旧世代的切片在 ③ 被物理删除，不会因"忘记清理"而堆积生效。

### 4.3 Python 失败路径

```
{"run_token":"...","status":"failed","error_code":"EMBEDDING_FAILED","message":"..."}
```

Go 执行（单事务）：

```sql
UPDATE ingest_jobs
   SET status='failed', error_code=$code, error_msg=$msg,
       finished_at=now(), updated_at=now()
 WHERE id=$job_id AND status='processing' AND current_run_token=$token;
-- 同样 CAS，0 行则丢弃

DELETE FROM doc_chunks
 WHERE doc_id=$doc_id AND run_token=$token AND visible = FALSE;   -- 清本次半成品
```

### 4.4 关于"清全世代"的安全性

Python 在阶段一执行 `DELETE FROM doc_chunks WHERE doc_id=$doc_id`（清全世代），而不是只清自己的 token，原因是：

1. **同 doc_id 的切片只可能属于一个"当前应该生效"的世代**。同一时刻只有一个执行者能持有该 doc 的 `run_token`（抢占互斥），并发的另一执行者必然拿不到 token，不会进入写库阶段。
2. 只清自己世代会让旧世代碎片长期滞留：重试路径下若新世代发布失败，库里会同时存在"旧世代可见切片"和"新世代不可见切片"，检索结果与状态判断都会变得难以推理。
3. 清全世代后即使本次失败，该文档也是"0 切片 + 非 completed"，语义单一：不可检索、可重试。这是安全的空状态。

代价：发布失败时旧世代数据也丢了，需要重新 Embedding（≤5MB 文档可接受）。**用"多花一次 Embedding"换"状态永远可判定"，值得。**

---

## 5. watchdog（决策 C）

进程内定时器，每 **60s** 一轮，单轮上限 **50** 条，单条处理在独立小事务内，避免长事务。

| 扫描对象 | 条件 | 动作 | 审计事件 |
|---|---|---|---|
| `pending` | `updated_at < now() - 120s` | CAS `pending → failed(DISPATCH_FAILED)` | `watchdog_reaped_pending` |
| `processing` | `COALESCE(started_at, updated_at) < now() - 150s` | CAS `processing → failed(TIMEOUT)`；清该 token 的 `visible=false` 切片 | `watchdog_reaped_processing` |
| `deleting` | `updated_at < now() - 60s` | 重试清理（调 Python 清向量 → 删行） | `deletion_retry` |
| 孤儿切片 | `visible=false` 且 `created_at < now() - 600s` | 物理删除 | `orphan_chunks_purged` |

**`updated_at` 是活列，不是装饰**：所有状态更新（抢占、发布、失败、转 `deleting`、清理重试）都必须 `SET updated_at = now()`。否则 `pending` 与 `deleting` 的收龄判断会永远停在建行时间，watchdog 要么立刻误杀、要么永不触发。

**`pending` 为什么按 `updated_at` 而不是 `created_at` 收龄**：`reingest` 把 `failed` 打回 `pending` 时会刷新 `updated_at`。若用 `created_at`，一篇搁置几天的文档一旦重试，会被下一轮 watchdog 立刻判 `DISPATCH_FAILED` —— 计时基准错了。建行时 `updated_at` 默认等于 `created_at`，首次派发场景不受影响。

**为什么 pending 和 processing 都要扫**：`pending` 意味着"Go 内存里的派发 goroutine 没跑起来或已消失"（进程崩溃、抢占未发生）。只扫 `processing` 会把这些文档永久卡住——v1.2 的 PRD 就犯过这个错。

**150s 的由来**：Go→Python 读超时 120s + 30s 余量。这个值**未经实测**（摄取耗时分布未知），先用保守值，实测后校准。

### 5.1 服务重启后的恢复行为

| 重启对象 | 后果 | 恢复动作 |
|---|---|---|
| Go | 所有在途派发丢失；`pending`/`processing` 均成为无主任务 | 启动完成后**立即跑一轮 watchdog**（不等 60s），把超期任务收敛为 failed |
| Python | 在途 `processing` 的执行者消失 | 同上；由 Go 的 watchdog 收敛。**V1 不续跑、不自动重试** |
| 两者同时 | 同上 | 同上 |

**明确不做**：重启后自动重跑。理由是重跑会把"用户以为失败"的任务变成"悄悄成功"，状态语义混乱；且没有队列无法保证重跑顺序。失败一律等用户 `reingest`。

### 5.2 不自动重试的决定

`failed` 不再自动重试。`attempt` 的作用退化为**执行世代标识**（区分第几次执行），不作为重试上限。

- 影响：PRD v1.2 的"`attempts` 达 3 后不再自动重试"需要改写 —— 已同步。
- 理由：无队列的自动重试会把同一篇文档反复烧 Embedding 费用，且用户看不到重试节奏。

---

## 6. 删除流程（决策 Q4）

### 6.1 为什么引入 `deleting`

不引入的替代方案是"先删向量再删记录"，但向量在 Python 侧、记录在 Go 侧，两端之间必然有窗口：窗口内文档记录不存在、向量还在 → 检索若按 `doc_chunks` 直查会召回幽灵片段；反之先删记录，向量变孤儿且无人清理。

`deleting` 用一个可见的中间态把窗口"显式化"：**检索立即不可见（状态非 completed），但清理未完成时用户能看到"删除中"，且 watchdog 会继续重试直到清干净。**

### 6.2 流程

```
DELETE /documents/{id}
  │
  ├─ 校验 owner（不匹配 → 404）
  ├─ CAS: status IN ('pending','processing','failed','completed') → 'deleting'（同时 updated_at=now()）
  │       同时 documents.deleted_at = now()，current_run_token = NULL（使在途执行失效）
  ├─ 立即返回 202 {doc_id, status:"deleting"}
  │
  └─ 后台清理（goroutine）：
       ① 调 Python DELETE /internal/v1/documents/{doc_id}/vectors
          → Python 按 doc_id 删除全部切片（含 visible=false）
       ② Go 单事务：DELETE ingest_jobs / document_contents / documents
       ③ 失败 → 保持 deleting，watchdog 每 60s 重试，超过 10 次记 error 日志（不自动放弃）
```

### 6.3 与在途执行的交互

| 时序 | 结果 |
|---|---|
| 删除时任务在 `processing` | `current_run_token` 置 NULL + 状态 `deleting`。Python 返回时 Go 的 CAS 因 `status != processing` 失败 → 丢弃，记 `stale_result_dropped` |
| Python 已写入 `visible=false` 切片后被删除 | 这些切片由 ① 按 `doc_id` 删掉（不依赖 token） |
| 删除中途 Go 崩溃 | 状态留在 `deleting`，watchdog 续做；用户看到"删除中"，可安全重试同一请求（幂等 202） |
| 删除中途 Python 不可用 | 同上；恢复后继续清 |
| 删除后历史消息引用该文档 | 消息引用是快照，`status=deleted`，展示"原文已删除"（PRD §7.7 / FR-21） |

### 6.4 硬性约束

1. **不允许只删 Go 侧记录**：`deleting` 未完成时绝不 `DELETE documents`，否则遗留无主向量。
2. Python 的向量清理必须**按 `doc_id` 全删**，不按 `run_token` 过滤 —— 兜住所有世代与孤儿。
3. `deleting` 状态下检索、`reingest`、读取正文一律拒绝（`409 DOC_DELETING` 或 404）。

---

## 7. 执行标识语义（决策 E）

| 项 | 规则 |
|---|---|
| **生成** | `crypto/rand` 16 字节 → 32 位小写 hex。不使用时间戳或 UUIDv1（可预测） |
| **存储** | `ingest_jobs.current_run_token`（当前执行）、`ingest_jobs.published_run_token`（已发布世代）；`doc_chunks.run_token`（数据归属） |
| **传递** | Go→Python 请求体字段 `run_token`；Python 响应原样回带；不放在 header，避免与 `X-Internal-Token` 混淆 |
| **校验点** | ① Go 发布时 CAS（必须）② Python 返回前自检 `current_run_token == 自己的 token`，不等则放弃返回 `status=stale`（省带宽，非安全必需）③ 删除时置 NULL 使在途失效 |
| **失效** | 新一次抢占覆盖 `current_run_token` 即让旧 token 失效；删除置 NULL；`published_run_token` 只在新发布时被覆盖 |
| **attempt** | 建行默认 **0**（未执行）；**每次抢占 +1**，值即"第几次执行"（首次执行后 = 1）；`reingest` 请求本身不递增；用于审计与 trace，不参与权限判断 |

---

## 8. 四类事件同时发生时的裁决（决策 D）

优先级从高到低：**删除 > 完成 > 超时收敛 > 重试**。

| 场景 | 裁决 | 依据 |
|---|---|---|
| 超时收敛 vs 迟到成功 | 迟到成功 CAS 失败 → 状态保持 `failed(TIMEOUT)`，切片不可见并由孤儿清理删除 | 谁先提交终态谁算数；用户可 reingest |
| 迟到成功 vs 新执行成功 | 旧 token 已被覆盖 → 旧结果必然丢弃 | `run_token` 版本隔离 |
| 重试 vs 删除 | `deleting` 拒绝 `reingest`（409 `DOC_DELETING`） | 删除优先级更高 |
| 删除 vs 完成 | `deleting` 时 CAS 要求 `status='processing'` 不满足 → 完成被丢弃 | 同上 |
| 两个 watchdog 实例同时收敛 | CAS 只有一方 0→1，另一方 0 行 | 条件更新天然互斥 |
| 删除失败后用户再次删除 | 202 幂等，不重复创建清理任务 | 清理任务可重入 |

---

## 9. 不做什么（避免范围膨胀）

| 不做 | 原因 |
|---|---|
| `POST /internal/v1/ingest/{id}/abort` | Q2 明确：统一 watchdog 收尾，V1 无主动取消的业务需求（用户取消上传 = 删除文档，已由 `deleting` 覆盖） |
| 分布式锁 / Redis / MQ | 条件更新 + 单实例足够；引入即超范围 |
| 断点续传（从第 N 个切片继续） | ≤5MB 文档重跑成本可接受 |
| 多实例部署下的 leader 选举 | V1 单实例；多实例需把 watchdog 改为 `SKIP LOCKED` 批量抢占，属 V2 |
| 自动重试 | 见 §5.2 |

---

## 10. 已知风险

| # | 风险 | 影响 | 现状 |
|---|---|---|---|
| R1 | 150s 的 `processing` 超时未实测 | 长文档可能被误判超时 | 待 S1 SPIKE 用真实文档校准 |
| R2 | Go 单实例 watchdog | 多副本部署会重复收敛（CAS 保证不写错，但会有无效扫描） | V1 单实例，V2 再说 |
| R3 | `visible=false` 切片占用存储直到清理 | 频繁失败时短期膨胀 | 孤儿清理 600s 阈值；未做容量监控 |
| R4 | 删除清理依赖 Python 可达 | Python 长期不可用时 `deleting` 长期滞留 | watchdog 无限重试，仅告警；未做降级 |
| R5 | 发布事务与 Python 提交之间的窗口 | 若 Go 在 Python 提交后、发布前崩溃，切片以不可见状态滞留 ≤600s | 由孤儿清理兜底，不丢数据不错数据 |

---

## 11. 修订记录

### v1.1（2026-10-22 19:40）— 审查修复 F1–F6

| # | 问题 | 修复 |
|---|---|---|
| F2 | 抢占条件 `IN ('pending','failed')` 可绕过 `reingest` 复活失败任务 | 改为 `WHERE status='pending'`，并补"为什么只允许 pending"的说明 |
| F3 | SQL 里的 `last_error` 列不存在 | 全部改为 `error_code` / `error_msg`（与 DDL 一致） |
| F5 | `updated_at` 从不被 SET，但 `deleting` 收龄依赖它 | 所有 CAS 语句补 `updated_at=now()`；`pending` 收龄改按 `updated_at`（`reingest` 会刷新，避免搁置文档一重试就被误杀） |
| F4 | 零切片可发布 `completed`，击穿不变量 | 新增零切片闸门：`chunk_count=0` → `failed(EMPTY_CONTENT)`，不发布 |
| — | §2 残留疑问句、§4.1 疑问注释 | 改为断言句；新增 §4.4 解释"清全世代"的安全性与代价 |
| — | v1.0 版本 | 本文件首版为 v1.0，本次为 v1.1 |
**同轮追加修复（19:43）**

| # | 问题 | 修复 |
|---|---|---|
| G2 | 头部版本仍写 v1.0，状态行引用 PRD v1.3 / data-model v1.0 | 头部改 v1.1；交叉引用改 PRD v1.4 / data-model v1.1 / contract v1.1 / test-matrix v1.1 |
| G3 | `attempt` off-by-one：DDL `DEFAULT 1` + 抢占 +1 → 首次执行记成 2，与用例期望冲突 | 语义定为"已执行次数"：DDL `DEFAULT 0`、`CHECK >= 0`，抢占 +1，首次执行后为 1 |
