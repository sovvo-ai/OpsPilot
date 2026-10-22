# 摄取相关 API 契约（V1）

| 项 | 值 |
|---|---|
| 版本 | v1.1 |
| 状态 | **已冻结（2026-10-22 评审通过）。仅允许 S1 SPIKE 实测结果触发修改；纯措辞/结构性打磨一律拒绝** |
| 范围 | 文档上传/状态/重试/删除 + Go→Python 内部摄取接口 |
| 上游 | `docs/prd/PRD.md` v1.4 §5.5–§5.8、`docs/architecture/ingest-consistency.md` v1.1 |
| 说明 | 本文件是**语义契约**；机器可读的 `openapi-*.yaml` 在 S2 由本文转写，字段名以本文为准 |

> 全局：`Authorization: Bearer <JWT>`（`/health/*` 与 `/internal/*` 除外）；`/internal/*` 用 `X-Internal-Token`，`hmac.compare_digest` 校验，失败 401 `INTERNAL_AUTH_FAILED`。所有响应带 `X-Request-Id`。

---

## 1. 对外接口

### 1.1 上传文档 `POST /api/v1/documents`

**请求**：`multipart/form-data`
| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `file` | file | 是 | `.md` / `.txt`，≤5MB，UTF-8 或 GBK |
| `title` | string | 否 | 缺省取文件名（NFC 规范化后 ≤200 字） |

**响应 202**
```json
{
  "doc_id": "8f3c...",
  "ingest_status": "pending",
  "deduplicated": false,
  "poll_url": "/api/v1/documents/8f3c..."
}
```

**重复上传（同 owner 同内容）** → 仍 202，但：
```json
{
  "doc_id": "8f3c...",
  "ingest_status": "failed",
  "deduplicated": true,
  "retry_hint": "POST /api/v1/documents/8f3c.../reingest",
  "error_code": "EMBEDDING_FAILED"
}
```
> 既有文档为 `completed` 时 `deduplicated=true` 且 `ingest_status=completed`，不带 `retry_hint`。

**去重命中的状态码统一为 `202`**（与新建一致），理由：

- 同一端点（`POST /documents`）响应形态保持一致，前端只处理一种状态码，不必为"命中去重"单独分支。
- 严格 REST 语义下"没有新资源被创建"更接近 `200`，但本接口的语义是"请求已被接受，文档处于某状态"，`202` 表达"已受理、状态待查"更贴合异步模型。
- 若改用 `200`，会与"上传成功但立即返回 pending"的场景产生两套码，收益不足以抵消复杂度。

**这条在三份文档中必须一致**：本文件、`PRD.md` FR-07、`00-environment-and-plan.md` §7.2。

**错误**：415 `UNSUPPORTED_FORMAT` / 413 `DOC_TOO_LARGE` / 422 `ENCODING_ERROR` / 422 `EMPTY_CONTENT` / 401 `UNAUTHORIZED`
**关键约束**：以上 4xx 均**不落库**（`documents` 无新行）。响应必须在 1s 内返回，不等摄取。

---

### 1.2 查询状态 `GET /api/v1/documents/{doc_id}`

**响应 200**
```json
{
  "doc_id": "8f3c...",
  "title": "k8s-troubleshooting.md",
  "ingest_status": "processing",
  "attempt": 2,
  "chunk_count": 0,
  "error_code": null,
  "error_msg": null,
  "created_at": "2026-10-22T19:30:00Z",
  "started_at": "2026-10-22T19:30:01Z",
  "finished_at": null
}
```

`ingest_status ∈ {pending, processing, completed, failed, deleting}`，判断规则：

| 状态 | 前端行为 |
|---|---|
| `pending` | 2s 轮询，不做任何"失败"暗示 |
| `processing` | 同上；可展示已耗时 |
| `completed` | 停止轮询，标记可检索 |
| `failed` | 停止轮询，展示 `error_msg` + 重试按钮（仅当可重试） |
| `deleting` | 停止轮询，展示"删除中"，隐藏重试按钮 |

`?include_content=true` 时额外返回 `content`（仅 `completed` 允许；`deleting` → 409 `DOC_DELETING`）。

---

### 1.3 重试摄取 `POST /api/v1/documents/{doc_id}/reingest`

**请求**：空 body
**响应 202**
```json
{"doc_id":"8f3c...","ingest_status":"pending"}
```

| 当前状态 | 结果 |
|---|---|
| `failed` | 202，`failed → pending`。**响应不含 `attempt`**：它只在抢占时递增，此刻尚未执行，返回任何数值都会误导；需要看进度请调 `GET /documents/{id}` |
| `pending` | 409 `DOC_BUSY`（已在队列中，拒绝重复派发） |
| `processing` | 409 `DOC_BUSY` |
| `completed` | 409 `DOC_NOT_RETRYABLE` |
| `deleting` | 409 `DOC_DELETING` |
| 不存在 / 非本人 | 404 `NOT_FOUND` |

> 幂等说明：并发两次请求，第二次因状态已变 `pending` 而返回 409；不存在"两次都成功"的情况。

---

### 1.4 删除文档 `DELETE /api/v1/documents/{doc_id}`

**响应 202**
```json
{"doc_id":"8f3c...","status":"deleting"}
```

| 当前状态 | 结果 |
|---|---|
| `pending` / `processing` / `failed` / `completed` | 202，转 `deleting`，`current_run_token` 置 NULL |
| `deleting` | 202（幂等，不重复创建清理任务） |
| 不存在 / 非本人 | 404 `NOT_FOUND` |

**语义承诺**：返回 202 只代表"已受理删除"，不代表向量已清完。清理失败会由 watchdog 继续重试，用户侧状态停在 `deleting`。**绝不允许出现"记录已删但向量残留"**（见设计 §6.4）。

---

## 2. 内部接口（Go → Python）

统一前缀 `/internal/v1`，必须带 `X-Internal-Token` 与 `X-Request-Id`。AI 服务不映射宿主端口（本机调试除外）。

### 2.1 执行摄取 `POST /internal/v1/ingest`

**请求**
```json
{"job_id":"...","doc_id":"...","run_token":"<32 hex>","attempt":2}
```

**成功响应 200**
```json
{"run_token":"<原样回带>","status":"ok","chunk_count":37}
```

**失败响应 200**（业务失败用 200 传结构化结果，不用 5xx）
```json
{"run_token":"...","status":"failed","error_code":"EMBEDDING_FAILED","message":"batch 3 failed after 3 retries"}
```

**过期响应 200**（Python 自检发现 token 已非当前执行）
```json
{"run_token":"...","status":"stale"}
```

**契约约束**

0. **`chunk_count = 0` 必须被 Go 拒绝发布**：Go 收到 `status=ok` 但 `chunk_count=0` 时，不置 `completed`，改置 `failed(EMPTY_CONTENT)` 并清理不可见切片。否则会造出"completed 但零可见切片"的不一致状态（违反 `test-matrix.md` I-07 / A14②）。
1. Python **不得**写 `ingest_jobs`；只返回结果。
2. Python 写 `doc_chunks` 时必须 `visible=false`、`run_token=<请求中的 token>`。
3. Python 返回前应自检 `current_run_token == 本 token`，不等则返回 `stale`（省带宽；**不是**安全边界，安全边界是 Go 的 CAS）。
4. 幂等：同一 `run_token` 重复调用应结果一致（先删本 token 切片再写）。
5. 超时：Go 连接 3s / 读 120s。超时后 Go **不**改状态，交 watchdog。

**HTTP 错误**：401 `INTERNAL_AUTH_FAILED` / 422 `INVALID_INGEST_REQUEST`（缺字段）/ 503 `DB_UNAVAILABLE`

> **不提供** `POST /internal/v1/ingest/{id}/abort`。决策 Q2：V1 用统一 watchdog 收尾；"用户想取消上传"的业务动作等价于删除文档，已由 `deleting` 覆盖。没有实际业务需求支撑就不加。

### 2.2 清理向量 `DELETE /internal/v1/documents/{doc_id}/vectors`

**响应 200**
```json
{"doc_id":"...","deleted_chunks":37}
```

**契约约束**
1. **按 `doc_id` 全删**，不过滤 `run_token`、不过滤 `visible` —— 兜住所有世代与孤儿。
2. 幂等：文档无切片时返回 `deleted_chunks=0`，仍是 200。
3. 失败返回 503，由 Go 侧 watchdog 重试。

### 2.3 健康检查

| 端点 | 语义 | 鉴权 |
|---|---|---|
| `GET /health/live` | 进程存活，**永远 200** | 无 |
| `GET /health/ready` | 依赖就绪，失败 503 | 无 |
| `GET /health/deps` | 依赖明细（db / llm / embedding），不含密钥 | 无 |

---

## 3. 错误码（HTTP 层，唯一权威表见 PRD §9.2）

| code | HTTP | 触发 | 落库 | 可重试 |
|---|---|---|---|---|
| `UNSUPPORTED_FORMAT` | 415 | 非 md/txt | 否 | 否 |
| `DOC_TOO_LARGE` | 413 | >5MB | 否 | 否 |
| `ENCODING_ERROR` | 422 | 编码无法识别 | 否 | 否 |
| `EMPTY_CONTENT` | 422 | 空内容 | 否 | 否 |
| `DOC_BUSY` | 409 | `pending`/`processing` 时重试或删除冲突 | 是 | — |
| `DOC_NOT_RETRYABLE` | 409 | `completed` 时重试 | 是 | 否 |
| **`DOC_DELETING`** | **409** | `deleting` 时读正文/重试 | 是 | 删除幂等 202 |
| `NOT_FOUND` | 404 | 不存在或非本人 | — | — |
| `UNAUTHORIZED` | 401 | 无/错/过期 JWT | — | — |
| `INTERNAL_AUTH_FAILED` | 401 | 内部令牌不符 | — | — |
| `DB_UNAVAILABLE` | 503 | 数据库不可用 | — | 是 |

**本次新增**：`DOC_DELETING`（409）。其余沿用 `PRD.md` v1.4 §9.2（该表是 HTTP 错误码的唯一权威版本）。

---

## 4. 状态与 HTTP 的对应关系（一图看清）

| `ingest_status` | 上传 | 查询 | reingest | delete |
|---|---|---|---|---|
| （不存在） | 202 新建 | 404 | 404 | 404 |
| `pending` | 202 去重/已存在 | 200 | 409 `DOC_BUSY` | 202 |
| `processing` | — | 200 | 409 `DOC_BUSY` | 202 |
| `completed` | 202 去重 | 200（可含正文） | 409 `DOC_NOT_RETRYABLE` | 202 |
| `failed` | 202 去重 + `retry_hint` | 200 | **202** | 202 |
| `deleting` | — | 200（不含正文） | 409 `DOC_DELETING` | 202 幂等 |

---

## 5. 待 S2 转写为 OpenAPI 时的注意点

1. `ingest_status` 用 `enum` 固定五个值，避免前后端各写一套字符串。
2. `attempt` 语义是"已执行次数"（建行 0、抢占 +1）；`reingest` 响应不返回该字段，`GET /documents/{id}` 返回的是**当前已执行次数**。不要把它当"将要执行第几次"。
3. `202` 与 `200` 的区分要在 spec 里显式声明：上传/重试/删除均为 `202`。
4. 内部接口单独一份 spec（`openapi-ai.yaml`），不复用对外 spec 的 security scheme。

---

## 6. 修订记录

### v1.1（2026-10-22 19:40）

| # | 问题 | 修复 |
|---|---|---|
| F4 | 未定义 `chunk_count=0` 的处理 | 契约约束 0：Go 必须拒绝发布并置 `failed(EMPTY_CONTENT)` |
| F11 | 去重命中的 HTTP 码在三份文档中缺失 | 明确 **202**（与新建同码）+ 三条辩护理由 + "三份文档必须一致"的约束 |
**同轮追加修复（19:43）**

| # | 问题 | 修复 |
|---|---|---|
| G2 | 头部 v1.0；正文写"沿用 PRD v1.2" | 改 v1.1；改引用 PRD v1.4 §9.2；新增"上游"行 |
| G3 | `reingest` 响应返回 `attempt` 并标注"预期值"，与"抢占才 +1"矛盾 | **响应移除 `attempt`**；`GET` 返回的是"当前已执行次数"；§5 注 2 重写 |
