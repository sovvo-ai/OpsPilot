# 测试计划（V1）

| 项 | 值 |
|---|---|
| 版本 | v1.0 |
| 状态 | 已冻结（2026-10-22 S2 审查通过）。仅允许实测结果触发修改 |
| 上游 | `docs/prd/PRD.md` v1.7 §11、`docs/testing/test-matrix.md` v1.1、`docs/testing/spike-report.md` §9 |
| 当前执行状态 | 用例清单已就绪，**尚无业务代码，全部 `NOT RUN`** |

---

## 1. 分层

| 层 | 范围 | 依赖 | 命令 |
|---|---|---|---|
| U 单元 | 切片器、检索排序、预算、工具纯函数、错误映射 | 无外部依赖 | `cd ai-service && .venv/Scripts/python.exe -m pytest tests/unit -q` |
| I 集成 | 真实 PG、状态机、并发抢占、删除、隐私隔离 | 本地 PG（`docker compose -f deploy/docker-compose.dev.yml up -d`） | `pytest tests/integration -q` |
| C 契约 | Go ↔ Python（中文、超时、错误透传、令牌三态） | 两侧进程 | `cd backend && SPIKE_PY_URL=… SPIKE_PY_TOKEN=… go test ./test/ -v` |
| E 端到端 | 上传 → 提问 → 引用 → 轨迹 | 全栈 | `bash scripts/smoke_test.sh` |
| V 评测 | 35 题 RAG 指标 | 全栈 + 已摄取语料 | 见 `rag-eval-spec.md` §4 |

Go 侧另跑：`cd backend && go test ./... && go vet ./...`。

---

## 2. 全局测试约定（G1–G7，全部来自实测教训）

| # | 约定 | 为什么（实证来源） |
|---|---|---|
| **G1** | 外部服务必须用**独立进程**启动，测试前做就绪探活；**禁止** `(cmd &)` 子 shell 启动 | S1 首次契约测试 3 条假失败：后台 uvicorn 随 shell 退出被终止（`spike-report.md` §9 M2） |
| **G2** | 超时类用例必须加**耗时下限**断言（如 `< 1.5s 判为无效`） | 若连接直接失败，超时用例会瞬时通过，形成假阳性（同上 M3） |
| **G3** | 任何涉及 HNSW 的用例必须显式 `SET hnsw.ef_search` | 默认 40 会返回错误结果集，"断言通过"但结论错误（同上 M4、§1.5） |
| **G4** | 能用 SQL 断言的状态，一律直接查库断言，不信接口返回值自证 | 状态与数据一致性是本项目最大风险面（A14/A15） |
| **G5** | 并发用例**至少 100 轮**，统计是否出现过双成功 | 条件更新的竞态窗口窄，单轮跑不出来（test-matrix §8） |
| **G6** | 未执行的用例在报告与 README 里一律写 `NOT RUN`；**禁止**把"计划测试"写成"已通过" | PRD A12 |
| **G7** | **含中文的验收一律用 Python `httpx` 客户端**，禁止用 Windows shell `curl`；纯函数单测**不得充当装配点用例的替身** | S7 轮次审查者与实现者**双双踩坑**的实证：① 中文经 Windows shell 传参被转义/编码破坏，同一请求在 httpx 下正常、在 curl 下失败，浪费两轮定位；② S7-A/B/C 三处连续缺陷都是"**函数对、装配错**"——纯函数测试全绿，但调用点喂错了文本（router 不看 attach_log、端口提取只吃 question）。凡涉及"数据从哪来"的用例，必须走真实链路断言 |

补充约定：
- 时间相关用例通过**回拨 `created_at/updated_at/started_at`** 构造，不 `sleep 120s`。
- 集成测试用独立库 `ops_pilot_test`，每用例前清表，不复用开发库。
- 涉及模型的集成测试用**假 LLM**（固定 JSON 响应）；真实模型只出现在评测与冒烟。

---

## 3. 覆盖矩阵（验收项 → 层级）

| 验收项 | U | I | C | E | V |
|---|---|---|---|---|---|
| A1 文档摄取 + 重试 | ● | ● |  | ● |  |
| A2 检索质量 |  |  |  |  | ● |
| A3 引用 ID 有效率 | ● | ● |  | ● | ● |
| A3b 引用内容支持率 |  |  |  |  | ● |
| A4 拒答 |  | ● |  | ● | ● |
| A5 路由与确定性节点 | ● | ● |  |  | ● |
| A6 预算与抢占 | ● | ● |  |  |  |
| A7 工具安全 | ● | ● |  | ● |  | 用例见 `tool-policy.md` §6：<br>`ai-service/tests/unit/test_tool_policy.py`<br>`ai-service/tests/integration/test_tool_injection.py` |
| A8 轨迹 |  | ● |  | ● |  |
| A9 引用悬挂 |  | ● |  | ● |  |
| A10 异常矩阵 | ● | ● | ● | ● |  |
| A11 一键启动 |  |  |  | ● |  |
| A12 评测真实性 |  |  |  |  | ● |
| A13 身份与资源隔离 |  | ● |  | ● |  |
| A14 异步一致性不变量 |  | ● |  |  |  |
| A15 删除彻底性 |  | ● |  | ● |  |
| Q1–Q4 质量门 | ● | ● | ● |  |  |
| Q5 七项指标 |  |  |  |  | ● |

`test-matrix.md` 的 57 条用例是 I 层的主体，编号 C/T/W/R/D/P/I 与本文分层对应：C→I（并发）、W→I（恢复）、D→I（删除）、P→I（隐私）、I→I（不变量断言，每个集成用例结束后统一跑）。

---

## 4. CI 步骤（S3 落地）

```
1. go vet ./... && go test ./...
2. ruff check ai-service && pytest ai-service/tests/unit
3. docker compose -f deploy/docker-compose.dev.yml up -d && 等就绪
4. pytest ai-service/tests/integration
5. cd backend && go test ./test/（需要 ai-service 已启动）
6. 敏感信息扫描
```

**第 6 步的扫描范围必须排除 `docs/`**：冻结文档里存在 `sk-***` 示例与"禁止出现 sk- 前缀"的政策描述，全仓扫描会永久误报。同理，`subprocess` 等词的政策描述也在 `docs/` 内，静态检查只扫 `ai-service/app/`。

```bash
grep -rn "sk-[A-Za-z0-9._-]\{8,\}" backend ai-service deploy scripts  # 不含 docs/
grep -rn "subprocess\|os\.system\|eval(" ai-service/app/tools/        # 应为空
```

---

## 5. 明确不测（V1）

| 不测 | 原因 |
|---|---|
| 多副本部署下 watchdog 竞态 | V1 单实例（PRD §12） |
| 模型输出质量的主观评分 | 无法量化，用 7 项客观指标替代 |
| 真实主机诊断工具 | V1 不含该能力 |
| 前端视觉回归 | V1.1 用 U1–U10 人工冒烟替代 |

---

## 6. 测试数据

| 数据 | 位置 | 说明 |
|---|---|---|
| 种子语料（5 篇） | `scripts/seed_docs/` | 与 `qa_golden.jsonl` 的 Gold 严格对应 |
| 评测集（35 题） | `ai-service/evals/datasets/qa_golden.jsonl` | 生成时已脚本校验 Gold 子串存在 |
| 假 LLM 响应 | `ai-service/tests/fixtures/` | S3 建立 |
| 异常样本 | `test-matrix.md` 各用例内联构造 | 不落盘，避免陈旧 |
