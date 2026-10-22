# 架构总览（V1）

| 项 | 值 |
|---|---|
| 版本 | v1.0 |
| 状态 | 待外部审查（Claude） |
| 性质 | 薄文档：只给拓扑、两条关键时序、文档索引；细节在各专项文档 |

---

## 1. 拓扑

```
                    ┌─────────────────────────────┐
   Client ──JWT───► │  backend (Go 1.27 + Gin)    │  宿主 8081
                    │  · 唯一对外入口              │
                    │  · 任务状态唯一写者          │
                    │  · watchdog（60s）           │
                    └───────┬─────────────┬───────┘
                            │ SQL         │ HTTP + X-Internal-Token
                            ▼             ▼
                    ┌───────────────┐  ┌──────────────────────────┐
                    │ PostgreSQL 16 │  │ ai-service (Python 3.11) │  内网 8000（不映射宿主）
                    │ + pgvector    │  │ FastAPI + LangGraph 1.x  │
                    │ 宿主 55432    │  │ 切片/Embedding/检索/编排 │
                    └───────────────┘  └────────────┬─────────────┘
                            ▲                       │ HTTPS
                            │ 写 doc_chunks         ▼
                            └────────────── 阿里云百炼（qwen / text-embedding-v4）
```

| 组件 | 端口 | 职责 | 不做什么 |
|---|---|---|---|
| `backend` | 8081 | 认证、上传、状态机、watchdog、会话/消息/轨迹落库、对外 SSE 代理 | 不做解析/切片/向量计算 |
| `ai-service` | 8000（内网） | 解析、切片、Embedding、检索、LangGraph 编排、工具执行 | 不写 `ingest_jobs`，不直接对外 |
| `postgres` | 55432 | 业务数据 + 向量 + 任务状态 | 不做业务逻辑 |
| 百炼 | — | LLM + Embedding | — |

**端口选择依据**：本机 5432/6379/8080 已被占用（`00-environment-and-plan.md` §1.6）。不引入 Redis / MQ / K8s。

---

## 2. 时序一：上传到可检索

```
Client          Go                      PG                 Python           百炼
  │  POST /documents │                   │                   │               │
  ├─────────────────►│ 前置校验(格式/大小/编码/空)            │               │
  │                  │ 规范化+sha256(owner内去重)             │               │
  │                  ├─ 事务: documents + contents + job(pending) ──────────►│
  │◄──── 202 pending ┼───────────────────┤                   │               │
  │                  │ goroutine: claim(pending→processing, attempt+1, run_token)
  │                  ├──────────────────►│                   │               │
  │                  ├──── POST /internal/v1/ingest (token, run_token) ─────►│
  │                  │                   │                   ├─ embed ──────►│
  │                  │                   │◄── INSERT chunks(visible=false, run_token)
  │                  │◄──── {run_token, status:ok, chunk_count} ─────────────┤
  │                  ├─ 事务: CAS(run_token) → completed         │               │
  │                  │        + visible=true (本世代)            │               │
  │                  │        + DELETE 旧世代                    │               │
  │  GET /documents/{id} 轮询 ──────────►│                   │               │
  │◄──── completed ──┤                   │                   │               │
```

失败与竞态：见 `ingest-consistency.md` §4–§6（迟到结果、watchdog、删除）。

---

## 3. 时序二：一次问答

```
Client        Go                Python(LangGraph)               PG            百炼
  │ POST /sessions/{id}/messages │                              │              │
  ├────────────►│ 取最近4轮上下文 │                              │              │
  │             ├── POST /internal/v1/chat (or /chat/stream) ──►│              │
  │             │                 ├─ router(规则→LLM) ──────────┼─────────────►│
  │             │                 ├─ retrieval(+rewrite) ───────┼──► chunks    │
  │             │                 │   └ 全低分 → refuse → END   │              │
  │             │                 ├─ troubleshoot(工具,≤3) ─────┼─────────────►│
  │             │                 ├─ evidence(规则→LLM兜底) ────┼─────────────►│
  │             │                 └─ final(可直通) ─────────────┼─────────────►│
  │             │◄── TurnResult{final_text, citations, usage, trace} ──────────┤
  │             ├─ 落库: messages + agent_runs + run_steps + tool_calls        │
  │◄── 200/SSE ─┤                                                             │
```

预算：`max_steps=8`、`max_llm_calls=6`、`reserved_for_final=1`、`max_tool_calls=3`、墙钟 60s（节点超时合计 59s）。执行点见 `agent-workflow.md` §5。

---

## 4. 文档索引

| 文档 | 内容 | 状态 |
|---|---|---|
| `prd/PRD.md` | 需求、验收、范围 | **已冻结 v1.6** |
| `00-planning/00-environment-and-plan.md` | 环境实测、决策、SPIKE 清单 | **已冻结 v4.4** |
| `architecture/ingest-consistency.md` | 两阶段发布、状态机、watchdog、删除 | **已冻结 v1.1** |
| `architecture/data-model.md` | DDL、索引、迁移规范 | **已冻结 v1.1** |
| `architecture/agent-workflow.md` | State/节点/预算/trace/编排入口 | 待审查 v1.0 |
| `architecture/tool-policy.md` | 三工具契约与禁令 | 待审查 v1.0 |
| `api/ingest-contract.md` | 接口语义（人读） | **已冻结 v1.1** |
| `api/openapi-backend.yaml` / `openapi-ai.yaml` | 机器可读契约 | 待审查 v1.0 |
| `testing/spike-report.md` | S1 实测（21 项） | 已完成 |
| `testing/test-plan.md` | 测试分层与全局约定 | 待审查 v1.0 |
| `testing/test-matrix.md` | 57 条用例（全 NOT RUN） | **已冻结 v1.1** |
| `testing/rag-eval-spec.md` | 7 指标 + 35 题 | 待审查 v1.0 |

---

## 5. 关键取舍（指向 ADR 位置）

| 取舍 | 结论 | 详见 |
|---|---|---|
| Go 与 Python 的边界 | Go 管状态与对外，Python 管数据与智能 | `ingest-consistency.md` §1 |
| 切片发布机制 | `visible` 闸门 + `run_token` CAS | 同上 §4 |
| 删除 | 引入 `deleting` 中间态 | 同上 §6 |
| 不用 Redis/MQ | 条件更新 + 单实例 watchdog | 同上 §9 |
| 检索 | 纯向量 + 阈值，不做 rerank | PRD §4.3 |
| 工具 | 3 个纯函数，不触达主机 | `tool-policy.md` §7 |

---

## 6. 部署形态

`docker compose` 三个服务（postgres、ai-service、backend）+ `initdb` 挂载建扩展。镜像全部使用本地已有基础镜像（`pgvector/pgvector:pg16`、`python:3.11-slim`、`golang:1.27.2-alpine`、`alpine:3.20`），不依赖 Docker Hub 直连（本机不可达，见 S0 §1.5）。
