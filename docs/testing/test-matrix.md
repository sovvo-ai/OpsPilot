# 测试矩阵（摄取一致性专项，V1）

| 项 | 值 |
|---|---|
| 版本 | v1.1 |
| 状态 | **已冻结（2026-10-22 评审通过）。仅允许 S1 SPIKE 实测结果触发修改；纯措辞/结构性打磨一律拒绝** |
| 覆盖 | 并发抢占、超时与迟到结果、重试冲突、删除流程、隐私与隔离、状态机不变量 |
| 上游 | `docs/architecture/ingest-consistency.md` v1.1、`docs/api/ingest-contract.md` v1.1 |
| **执行状态** | **全部 `NOT RUN`** —— 项目尚无业务代码，本文件是待实现的用例清单，不含任何实测结论 |

> 标记说明：`IT` = 集成测试（真实 PG + 真实 HTTP，可 mock 外部模型）；`UT` = 单元测试（纯函数/协议逻辑）。
> 断言方式凡是能落到 SQL 的，一律用 SQL 直接查库断言，不靠接口返回自证。

---

## 1. 并发与抢占（决策 Q5）

| ID | 层级 | 场景 | 构造方式 | 期望 |
|---|---|---|---|---|
| C-01 | IT | 两个执行者同时抢占同一 `pending` 任务 | 并发两个 goroutine 调 claim；其中一个被 `sleep` 卡在 Python 调用上 | 只有一个 `RETURNING` 有行；另一个 0 行且不产生第二次 Python 调用（mock 计数 = 1） |
| C-02 | IT | 对 `processing` 任务再次抢占 | 人为把状态置 `processing` 后调 claim | 0 行；`attempt` 不变；审计 `claim_conflict` |
| C-03 | IT | 对 `completed` 任务抢占 | 同上 | 0 行；状态仍 `completed` |
| C-04 | UT | `attempt` 递增时机 | 按 `reingest → claim` 循环三轮（**不能直接连续 claim**：claim 只接受 `pending`） | 首次 1，其后 2、3；`reingest` 请求本身不改变 `attempt`，只有 claim 才 +1 |
| C-05 | IT | 两个 watchdog 周期重叠时收敛同一任务 | 手动并发两次 reap | 只有一次 `→failed`；另一次 0 行 |
| C-06 | IT | 对 `failed` 直接 claim | 手动把 job 置 `failed` 后调 claim（不经 reingest） | **0 行，状态不变**。验证抢占条件只接受 `pending`，杜绝"游离 goroutine 复活失败任务" |

---

## 2. 超时与迟到结果（决策 Q2 / D）

| ID | 层级 | 场景 | 构造方式 | 期望 |
|---|---|---|---|---|
| T-01 | IT | **未超时**的正常成功（对照基线） | Python 侧 `sleep(100s)`（< Go 读超时 120s）后写切片并返回 ok | 最终 `status=completed`，切片 `visible=true`；`attempt=1`；无 `stale_result_dropped` |
| T-01b | IT | 真实超时后，Python 仍返回 ok 的结果**不得**被发布 | Python `sleep(>120s)` 后仍返回 ok（Go 客户端已超时断开） | 最终 `status=failed(TIMEOUT)`；**该 doc 无 `visible=TRUE` 切片**；本次切片为 `visible=false` 并由孤儿清理删除。**审计应为 `watchdog_reaped_processing` + `orphan_chunks_purged`**——此场景下 Go 早已超时返回，**没有接收迟到响应的代码路径，CAS 不发生，因此不会产生 `stale_result_dropped`**（该事件只在 T-02 那种"响应仍在读超时内到达、但任务已被提前收敛"的构造下可观测） |
| T-02 | IT | watchdog 先判超时，Python 后返回 ok | 强制 watchdog 在 Python 返回前把 job 置 `failed(TIMEOUT)` | Python 返回后 Go 的 CAS 0 行 → 保持 `failed`；本次切片 `visible=false` 且随后被孤儿清理删除；审计 `stale_result_dropped` |
| T-03 | IT | 迟到结果 vs 新执行结果 | run A 慢、超时失败 → 用户 reingest 产生 run B 成功 → run A 返回 | run A 结果被丢弃；`published_run_token = token_B`；库中只有 B 的切片 |
| T-04 | IT | 全库不变量 | 上述用例跑完后执行断言 SQL | `SELECT count(*) FROM doc_chunks c JOIN ingest_jobs j ON j.doc_id=c.doc_id WHERE c.visible=TRUE AND j.status<>'completed'` = **0** |
| T-05 | IT | 反向不变量 | — | `SELECT count(*) FROM ingest_jobs j WHERE j.status='completed' AND NOT EXISTS (SELECT 1 FROM doc_chunks c WHERE c.doc_id=j.doc_id AND c.visible=TRUE)` = **0** |
| T-06 | IT | Python 超时后失败退出 | 超时后进程直接返回 500 / 断连 | 最终 `failed(TIMEOUT)`；该 doc 无 `visible` 切片；可 `reingest` 成功 |
| T-07 | UT | 超时阈值边界 | `started_at = now()-149s` / `now()-151s` | 149s 不收敛，151s 收敛 |
| T-08 | IT | 零切片不得发布（F4） | Python 返回 `{"status":"ok","chunk_count":0}` | **不置 `completed`**；置 `failed(EMPTY_CONTENT)`；该 doc 无 `visible` 切片；可 reingest |
| T-09 | IT | 发布后 `chunk_count` 与实际一致 | 发布完成后对比 `ingest_jobs.chunk_count` 与 `count(*) where visible` | 两者相等；不等即为缺陷 |

---

## 3. 恢复与 watchdog（决策 C）

| ID | 层级 | 场景 | 构造方式 | 期望 |
|---|---|---|---|---|
| W-01 | IT | `pending` 超 120s | 建 job 后不派发，推进时间 | `→failed(DISPATCH_FAILED)`；审计 `watchdog_reaped_pending` |
| W-02 | IT | `processing` 超 150s | `started_at` 回拨 | `→failed(TIMEOUT)`；清该 token 的 `visible=false` 切片 |
| W-03 | IT | Go 重启 | 进程在 `pending` 状态下被 kill 再启 | 启动后立即跑一轮 watchdog；超期任务收敛为 `failed`，不等 60s |
| W-04 | IT | Python 重启 | 在 `processing` 时重启 Python | 由 Go watchdog 收敛；**不自动重跑**（断言无第二次 ingest 调用） |
| W-05 | UT | 扫描批量上限 | 造 120 条超期任务 | 单轮最多处理 50 条，剩余留待下一轮 |
| W-06 | IT | 孤儿切片清理 | 造 `visible=false` 且 `created_at` 早于 600s | 被物理删除；未超时的不被删 |
| W-07 | IT | watchdog 幂等 | 对同一 `failed` 任务重复 reap | 0 行变化，无副作用 |
| W-08 | IT | `reingest` 后的 pending 不被立即误杀（F5） | 造一篇 `created_at` 在 3 天前的 failed 文档 → reingest | 新一轮 watchdog 不把它判 `DISPATCH_FAILED`（收龄按 `updated_at`）；`updated_at` 在 reingest 时被刷新 |
| W-09 | IT | `updated_at` 是活列 | 依次执行 claim / 失败 / 删除 CAS 后读取 `updated_at` | 每次状态变更后 `updated_at` 严格递增；若某次未变，说明 SQL 漏了 `SET updated_at=now()` |

---

## 4. 重试冲突（决策 D）

| ID | 层级 | 场景 | 期望 |
|---|---|---|---|
| R-01 | IT | `failed` → reingest | 202；`→pending`；随后 claim 成功且 `attempt+1` |
| R-02 | IT | `pending` 时 reingest | 409 `DOC_BUSY` |
| R-03 | IT | `processing` 时 reingest | 409 `DOC_BUSY` |
| R-04 | IT | `completed` 时 reingest | 409 `DOC_NOT_RETRYABLE` |
| R-05 | IT | 并发两次 reingest | 一次 202、一次 409；库中只有一次 `pending` 迁移 |
| R-06 | IT | 重试后切片不重复 | 首次成功 37 片 → 人为置 failed → reingest 成功 | 切片总数仍 37（`count(*) where visible`），旧世代被清 |
| R-07 | IT | 重复上传已 `failed` 文档 | 202 + `deduplicated=true` + `retry_hint`；不触发新摄取（mock 计数不变） |

---

## 5. 删除流程（决策 Q4）

| ID | 层级 | 场景 | 期望 |
|---|---|---|---|
| D-01 | IT | 删除 `completed` 文档 | 202 `deleting`；清理后三表无残留（documents / document_contents / ingest_jobs）+ `doc_chunks` 为 0 |
| D-02 | IT | 删除 `processing` 文档 | 202；`current_run_token` 置 NULL；Python 迟到返回被丢弃；清理后无残留 |
| D-03 | IT | 删除时 Python 不可用 | 状态停留 `deleting`；watchdog 每轮重试；恢复后清理成功 |
| D-04 | IT | 重复删除 | 两次均 202，第二次不创建新清理任务 |
| D-05 | IT | `deleting` 中 reingest | 409 `DOC_DELETING` |
| D-06 | IT | `deleting` 中读正文 | 409 `DOC_DELETING`（`?include_content=true`） |
| D-07 | IT | 删除后检索不召回 | 立即不召回（状态非 completed），清理后彻底消失 |
| D-08 | IT | 删除后历史消息 | 200，引用 `status=deleted`，显示"原文已删除" |
| D-09 | IT | 删除中途 Go 崩溃 | 重启后 watchdog 续做，最终无残留 |
| D-10 | IT | 仅删记录不删向量的防线 | 断言：不存在 `documents` 无对应 `doc_chunks` 却曾 `completed` 的静默状态（以 `deleted_at IS NULL` 且 `status<>'completed'` 且 chunks>0 反查） |

---

## 6. 隐私与隔离（决策 Q6）

| ID | 层级 | 场景 | 期望 |
|---|---|---|---|
| P-01 | IT | 跨用户相同内容上传 | `demo-user` 与 `test-user-2` 各自建档，两行 `documents`；**不返回对方 `doc_id`**；`deduplicated=false` |
| P-02 | IT | 跨用户去重不泄露存在性 | 用 B 的 token 上传 A 已上传的完全相同内容后，响应中不含任何 A 的标识 |
| P-03 | IT | 跨用户读取/删除/重试 | 全部 404（文档、正文、reingest、delete） |
| P-04 | IT | 跨用户检索 | B 的检索结果中不出现 A 的 `doc_id` / `chunk_id` |
| P-05 | IT | `source_hash` 不外泄 | `GET /documents/{id}`、列表、错误响应中均无 `source_hash` 字段 |
| P-06 | UT | 日志不含指纹 | 触发一次完整摄取，抓日志断言无 64 位 hex 指纹、无正文、无 token 明文（`run_token` 允许出现在审计字段？→ **不允许**，只允许 `job_id`） |
| P-07 | IT | 删除后指纹消失 | 删除文档后，`documents` 行不存在，指纹随之消失；无独立指纹表（断言库中不存在额外指纹表） |

---

## 7. 状态机不变量（每次集成测试结束统一跑）

| ID | 断言 |
|---|---|
| I-01 | `ingest_jobs.status` 只取五个枚举值之一 |
| I-02 | `completed` 必有 `published_run_token` 且非空 |
| I-03 | `chunk_count > 0` 时状态必为 `completed` |
| I-04 | `processing` 必有 `current_run_token` 与 `started_at` |
| I-05 | 不存在 `doc_id` 对应的多条 `ingest_jobs`（UNIQUE 保证） |
| I-06 | `deleting` 的文档 `deleted_at` 必非空 |
| I-07 | 所有 `visible=true` 的切片，其 `doc_id` 对应任务状态 **∈ {`completed`, `deleting`}`**（`deleting` 窗口内允许 `visible=TRUE`；检索由 FR-16 双重过滤排除，不构成正确性缺陷。见 v1.2 修订） |
| I-08 | 所有 `visible=true` 的切片，其 `run_token = published_run_token` |
| I-09 | **删除窗口内断言不误报**：文档处于 `deleting` 且仍有 `visible=TRUE` 切片时，I-07（修正口径）计数必须为 **0**，同时 FR-16 检索 SQL 必须不返回该文档任何切片（证明"过滤而非删除"才是正确性保证） |

---

## 8. 执行与记录要求

- 每条用例的通过判定必须来自真实运行输出（SQL 结果 / HTTP 状态 / mock 计数）。
- **未执行的用例保持 `NOT RUN`**，不得在报告或 README 中写成"已验证"。
- 集成测试跑在独立库（`ops_pilot_test`），不复用开发库；每个用例前清表。
- 时间相关用例（W-01/02、T-07）通过回拨 `created_at/started_at` 实现，不使用真实 `sleep 120s`，避免测试过慢。
- 并发用例（C-01、C-05）至少跑 100 轮，统计是否出现过双成功。

---

## 9. 修订记录

### v1.2（2026-10-23）— A14① 断言口径修正（外部审查裁定）

| # | 问题 | 修复 |
|---|---|---|
| I-07 修 | 原 I-07 写"所有 `visible=true` 切片的对应任务状态为 `completed`"，**漏了 `deleting` 窗口**。实测证实：文档进入 `deleting`（`deleted_at` 已置位）后，旧世代 `visible=TRUE` 切片在 watchdog 收敛前仍存在，此时原口径计数为 **1**（非 0），属断言文案缺陷 | I-07 改为 `status ∈ {completed, deleting}`；PRD A14① 同步 |
| I-09 增 | 缺"删除窗口内执行断言不误报"的用例 | 新增 I-09：`deleting` + `visible` 切片共存时修正口径必须为 0，且 FR-16 检索 SQL 必须不返回该文档任何切片 |

> **性质说明**：正确性无洞——检索由 FR-16 双重过滤（`visible=TRUE` ∧ `status='completed'` ∧ `deleted_at IS NULL` ∧ `owner_id`）保证，`deleting` 文档的可见切片从不参与检索。缺陷仅在**断言文案**：口径过严会误报。来源=外部审查裁定。

**用例总数**：57 → **58 条**（新增 I-09）。

### v1.1（2026-10-22 19:40）

| # | 问题 | 修复 |
|---|---|---|
| F1 | T-01 期望"超时后成功 → completed"，与 v1.3 协议矛盾，用例不可能通过 | T-01 改为"未超时（sleep 100s）正常成功"；新增 T-01b 断言"超时后成功不得被发布" |
| F2 | C-04 描述"连续 3 次 claim"不可执行（claim 只接受 pending） | 改为 `reingest → claim` 循环三轮；新增 C-06 验证"failed 直接 claim 必须 0 行" |
| F4 | 缺少零切片用例 | 新增 T-08（零切片不发布）、T-09（`chunk_count` 与实际一致） |
| F5 | 缺少 `updated_at` 与收龄基准用例 | 新增 W-08（reingest 后不被误杀）、W-09（`updated_at` 单调递增） |

**用例总数**：v1.0 的 51 条 → v1.1 的 **57 条**（新增 C-06、T-01b、T-08、T-09、W-08、W-09）。

| 组 | v1.0 | v1.1 |
|---|---|---|
| C 并发 | 5 | 6 |
| T 超时/一致性 | 7 | 10 |
| W 恢复 | 7 | 9 |
| R 重试 | 7 | 7 |
| D 删除 | 10 | 10 |
| P 隐私 | 7 | 7 |
| I 不变量 | 8 | 8 |
| **合计** | **51** | **57** |

**执行状态仍全部 `NOT RUN`**。
**同轮追加修复（19:43）**

| # | 问题 | 修复 |
|---|---|---|
| G1 | T-01b 期望 `stale_result_dropped` 不可达——真实超时时 Go 已断开，无人接收迟到响应，CAS 不发生 | 期望改为 `watchdog_reaped_processing` + `orphan_chunks_purged`；并注明 `stale_result_dropped` 只在 T-02 类构造下可观测 |
| G2 | 头部 v1.0 | 改 v1.1，新增"上游"行 |
| G3 | C-04 / T-01 的 attempt 期望依赖新语义 | 随 G3 统一为"已执行次数"，用例期望不变（首次 1） |
