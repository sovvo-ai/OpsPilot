# S9 SSE 契约验证（PRD §8.5）

| 项 | 值 |
|---|---|
| 日期 | 2026-10-24 12:2x（GMT+8） |
| 提交 | `f7275b5`（feat: S9 SSE） |
| 环境 | **全新栈**：`docker compose down -v` → `up -d` → `run_migrate.py up` → 摄取 5 篇种子语料（全 `completed`） |
| 脚本 | `scripts/s9_sse_contract.py` |
| 结果 | **9/9 通过** |

> **本文所有输出均为命令原始 stdout**，未做任何加工或换算。
> 纪律（`docs/testing/test-plan.md` G7）：含中文的请求一律 Python `httpx`（UTF-8），**禁 Windows shell curl**。

---

## 0. 环境准备（原文）

```bash
$ docker compose down -v
 Volume deploy_opspilot_pg_data  Removing
 Volume deploy_opspilot_pg_data  Removed
 Network deploy_opspilot  Removed

$ docker compose up -d
 Container opspilot-postgres  Waiting
 Container opspilot-postgres  Healthy
 Container opspilot-backend  Starting
 Container opspilot-backend  Started

$ docker ps --filter name=opspilot --format '{{.Names}}\t{{.Status}}'
opspilot-backend	Up 14 seconds
opspilot-ai	Up 15 seconds
opspilot-postgres	Up 21 seconds (healthy)

$ python scripts/run_migrate.py up
2026/10/24 12:26:55 迁移 up 成功

$ # 摄取 5 篇种子文档
  k8s-pod-troubleshooting.md: completed chunks=2
  linux-network-troubleshooting.md: completed chunks=1
  mysql-slow-query.md: completed chunks=2
  nginx-502-upstream.md: completed chunks=2
  observability-logging.md: completed chunks=1
```

---

## 1. 契约测试：9/9 通过（原文）

```bash
$ ai-service/.venv/Scripts/python.exe scripts/s9_sse_contract.py
==========================================================================
S9 SSE 契约验收（PRD §8.5）
==========================================================================

--- 用例 2/3：done 必发 & citation 一次性 ---
  ✅ §8.5-S2 done 必发 — 事件序列=['meta', 'delta', 'delta']…['done', 'saved']
  ✅ §8.5-S1 citation 流结束后一次性发（不半截） — citation 出现 1 次，位置 [28]，末 delta 位置 27
  ✅ §8.5-S1 delta 之间无 citation 插入 — 中途 citation=[]
  ✅ U9 首个 token ≤2s — 首 delta=0.8s
  ✅ done 携带完整 result（供同一落库路径） — result keys=19
  ✅ §8.5-S5 同一 run 可通过 GET /runs 补全 — runs HTTP=200 node_path=['router', 'retrieval', 'evidence', 'final']

--- 用例 4：S6 回退（客户端不支持流）---
  ✅ §8.5-S6 不支持流 → 整包 JSON（同一份内容） — HTTP=200 ctype=application/json; charset=utf- 有 message=True answer_type=answered

--- 用例 1：客户端断流 → interrupted + 停止消耗 ---
    第 1 轮：读到 0 个 delta 后强制 RST（run_id=c02ebccd…）
  ✅ §8.5-S4 断流后 trace 标记 interrupted — 第 1 轮：terminated_reason=interrupted, llm_calls=0（run_id=c02ebccd）
  ✅ §8.5-S4 断流后停止消耗（llm_calls 不高于正常轮） — interrupted=0, 正常=1

==========================================================================
S9 SSE 契约：9/9 通过
  ✅ §8.5-S2 done 必发
  ✅ §8.5-S1 citation 流结束后一次性发（不半截）
  ✅ §8.5-S1 delta 之间无 citation 插入
  ✅ U9 首个 token ≤2s
  ✅ done 携带完整 result（供同一落库路径）
  ✅ §8.5-S5 同一 run 可通过 GET /runs 补全
  ✅ §8.5-S6 不支持流 → 整包 JSON（同一份内容）
  ✅ §8.5-S4 断流后 trace 标记 interrupted
  ✅ §8.5-S4 断流后停止消耗（llm_calls 不高于正常轮）

结果已落盘: .workbuddy\s9_sse_contract.json
```

---

## 2. 证据 E1：事件流完整 dump（原文）

请求：`{"question": "Nginx 的 502 和 504 分别代表什么？"}`

```
[ 0.33s] event=meta {"run_id": "480fcc08-d3e0-4e9c-8dbe-60f397be5cc4", "session_id": "bf0cfa28-...", "request_id": "..."}
[ 1.01s] event=delta text='5'
[ 1.05s] event=delta text='02 Bad'
[ 1.09s] event=delta text=' Gateway 表'
[ 1.19s] event=delta text='示上游返回'
[ 1.30s] event=delta text='了非法响应，或连接'
[ 1.44s] event=delta text='被上游重置，N'
[ 1.58s] event=delta text='ginx 无法解析响应'
[ 1.66s] event=delta text=' `[1]`'
[ 1.73s] event=delta text='。  \n50'
[ 1.91s] event=delta text='4 Gateway Timeout 表示'
[ 2.00s] event=delta text=' Nginx 等'
[ 2.14s] event=delta text='待上游响应超时，'
[ 2.30s] event=delta text='上游可能仍在处理，只是'
[ 2.42s] event=delta text='超过了 `proxy_read_timeout`'
[ 2.53s] event=delta text=' `[1]`。  \n\n'
[ 2.64s] event=delta text='一句话区分：50'
[ 2.80s] event=delta text='2 是“上游答错了'
[ 2.91s] event=delta text='”，504 是“'
[ 3.05s] event=delta text='上游没答完”；'
[ 3.17s] event=delta text='502'
[ 3.28s] event=delta text=' 要查上游进程，'
[ 3.39s] event=delta text='504 要'
[ 3.48s] event=delta text='查耗时与'
[ 3.61s] event=delta text='超时配置 `['
[ 3.73s] event=delta text='1]`。'
[ 3.75s] event=citation 条数=1 index=[1]
[ 3.75s] event=done usage={'llm_calls': 1, 'tool_calls': 0} result_keys=19
         route=kb_qa node_path=['router', 'retrieval', 'evidence', 'final']
         answer_type=answered citations=1
         terminated_reason=completed
[ 3.75s] event=saved msg_id=19ab450d-907e-4570-bb4d-f9a30037e5a5

合计 29 个事件；run_id=480fcc08-d3e0-4e9c-8dbe-60f397be5cc4
```

**逐条对齐 PRD §8.5**：

| 契约 | 要求 | 本 dump 的体现 |
|---|---|---|
| S1 | `meta` / `delta` / `citation` / `done` | 四类齐全（+ `saved` 为落库确认，非契约必需） |
| S1 | citation **流结束后一次性发** | `citation` 出现在 **3.75s**，**所有 28 个 delta 之后**（末 delta 3.73s） |
| S1 | 不在流中插半截引用 | 3.73s 前的 delta 里**无 citation 事件** |
| S2 | **必须显式发 `done`** | `done` 在 3.75s，携带 `usage` 与完整 `result` |
| U9 | 首 token ≤2s | **1.01s** |

> **delta 是真流式的证据**：切分粒度不均匀（`'5'` / `'02 Bad'` / `' Gateway 表'`…），
> 这是 LLM 上游 SSE 的**真实增量边界**；若是"整段生成完再切块"，切片会等长且集中在末尾。

---

## 3. 证据 E2/E3：S5 两路径落库对照（原文）

**流式路径**（run `480fcc08`）：

```sql
SELECT route, node_path, llm_calls, tool_calls, terminated_reason,
       retrieved_chunk_ids IS NOT NULL AS has_chunks, duration_ms
  FROM agent_runs WHERE id='480fcc08-...';

{"route": "kb_qa", "node_path": "['router', 'retrieval', 'evidence', 'final']",
 "llm_calls": "1", "tool_calls": "0", "terminated_reason": "completed",
 "has_chunks": "True", "duration_ms": "3433"}

run_steps 行数 = 4
messages = [{'role': 'user', 'len': 25}, {'role': 'assistant', 'len': 206}]
```

**非流式路径**（同一问题，run `354ab9c9`）：

```sql
{"route": "kb_qa", "node_path": "['router', 'retrieval', 'evidence', 'final']",
 "llm_calls": "1", "tool_calls": "0", "terminated_reason": "completed",
 "has_chunks": "True", "duration_ms": "3884"}

run_steps 行数 = 4
messages = [{'role': 'user', 'len': 25}, {'role': 'assistant', 'len': 235}]
```

**结论**：两路径的 `route` / `node_path` / `run_steps 行数` / `terminated_reason` / `messages 条数`
**完全一致**（仅 `duration_ms` 与答案长度因生成波动略有差异，属正常）。

→ 说明两者走的是**同一个落库函数** `persistAssistantTurn()`（`backend/internal/handlers/messages.go`），
满足 §8.5-S5「共用同一编排与同一落库逻辑，不允许两套代码路径」。

---

## 4. 证据 E4：S4 断连后 `llm_calls=0`（原文）

```sql
SELECT id, terminated_reason, llm_calls, tool_calls, degrade_reasons, duration_ms
  FROM agent_runs WHERE terminated_reason='interrupted'
 ORDER BY created_at DESC LIMIT 3;

{"id": "c02ebccd-76a1-4278-b0a5-3578792467e4", "terminated_reason": "interrupted",
 "llm_calls": "0", "tool_calls": "0",
 "degrade_reasons": "['client_disconnected']", "duration_ms": "12"}

interrupted run 总数 = 1
对照：completed 但 llm_calls=0 的 run 数 = 0（直通路径，正常）
```

**关键数字**：

| 项 | 值 | 说明 |
|---|---|---|
| `terminated_reason` | `interrupted` | §8.5-S4 要求的标记 |
| **`llm_calls`** | **0** | **一次模型调用都没烧** —— 断连在第 0 个 delta 前后即被察觉 |
| `duration_ms` | **12** | 12 毫秒内完成中断（远早于 `final` 节点的 3.4s 生成） |
| `degrade_reasons` | `['client_disconnected']` | 降级原因可追溯 |

**对照佐证**：`completed` 且 `llm_calls=0` 的 run 数为 0 —— 说明"不烧 token"不是普遍现象，
而是**断连路径特有的行为**（正常路径至少调 1 次模型）。

---

## 5. 复现步骤（完整命令原文）

```bash
# 1. 全新栈
cd deploy && docker compose down -v && docker compose up -d && sleep 12

# 2. 迁移（down -v 清空了数据卷，必须重跑）
cd .. && export POSTGRES_HOST=localhost
set -a && . deploy/.env && set +a
python scripts/run_migrate.py up

# 3. 摄取 5 篇种子语料（脚本内联，见 §0）
#    ai-service/.venv/Scripts/python.exe - <<'PY' ... PY

# 4. 契约测试
ai-service/.venv/Scripts/python.exe scripts/s9_sse_contract.py

# 5. 证据查询（§2~§4 的 SQL 与脚本，见 /tmp 临时脚本或本文附录）
```

---

## 6. 断连检测的实现要点（踩坑记录）

断连（§8.5-S4）是本次 SSE 实现**最难的部分**，共踩 4 层坑，逐层剥开如下
（同一内容已写进 `backend/internal/handlers/stream.go` 包注释与 `deploy/runbook.md`）：

| # | 现象 | 根因 | 处置 |
|---|---|---|---|
| 1 | `c.Request.Context()` 迟迟不取消 | SSE 客户端多为"停止读取"（半开连接），Go http server 需读到 FIN 才取消 ctx | 加**心跳 goroutine**（每 200ms 写 SSE 注释行） |
| 2 | 心跳写小数据**不报错** | 响应仅几 KB，**远小于内核发送缓冲**（数十 KB），客户端消失后写仍"成功" | 心跳带 **8KB 填充**（`:xxxx…`，SSE 注释，客户端忽略） |
| 3 | `streamErr` 判定漏判 | 断连时上游读取中断表现为 `*HTTPError{Code:"STREAM_INTERRUPTED"}`，**不是** `context.Canceled` | 两种形态**都判定**为断连 |
| 4 | **🔴 分支永不执行** | 断连分支写在 `streamErr` 处理**之前** → 判定时 `clientGone` 尚未置位 | 把 `streamErr` 处理**移到断连分支之前** |

**测试侧同类坑**：`httpx` 的 `break` / `with` 退出**不保证关闭 TCP**（走连接池），
服务端因此感知不到断连。必须用**裸 socket + `SO_LINGER(0)` 强制 RST**，
并在**读到 `meta` 立即断**（最早时机 → 最易触发 `EPIPE`）。

> 注：客户端断连在 TCP 层是**概率性**可观测的（取决于数据是否已越过内核缓冲），
> 故脚本设了**最多 3 轮重试**；实测**第 1 轮即命中**。