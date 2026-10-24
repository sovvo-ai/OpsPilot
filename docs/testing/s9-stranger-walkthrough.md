# 陌生人 + 面试官双视角体验记录（README → GIF → clone → up）

| 项 | 值 |
|---|---|
| 日期 | 2026-10-24 15:0x（GMT+8） |
| 视角 | ① 陌生人：只照 README 抄命令；② 面试官：只看 README 前 30 行 + GIF |
| 结论 | **全流程通过**，无卡点；修出 1 处口径不一致（已在 README 统一） |

---

## 视角 ①：陌生人照 README 逐条执行

**执行方式**：先 `docker compose down -v` 清空数据卷，再从 README §1 开始逐步照抄（**不改环境、只改 README** —— 若卡住就是文档问题）。

| 步 | README 命令 | 实际结果 | |
|---|---|---|---|
| 0 | 前置检查 | Docker Desktop 已启动；`python --version` → `Python 3.11.9` | ✅ |
| 1 | `cp deploy/.env.example deploy/.env` | `.env.example` 存在，含 `DASHSCOPE_API_KEY` 占位 | ✅ |
| 2 | `docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build` | 起栈成功；**从 down -v 到两个服务 ready = 14s** | ✅ |
| 3 | `python -m venv ai-service/.venv` + `pip install -r requirements.txt` | venv 为 Python 3.11.9；`requirements.txt` 存在 | ✅ |
| 3b | （迁移）—— 见下方补充 | `schema_migrations` 由 backend 启动时**自动**建好（0001~0003） | ✅ |

> **补充（2026-10-24 二次核实）**：第 3 步**不需要手动迁移**。实测 `down -v` 后只执行
> `docker compose up -d`，backend 启动时自动调用 `MigrateUp()`，`schema_migrations` 三行齐备，
> **直接跑 smoke 即通过**（未手动执行 `run_migrate.py`）。
> 因此 README 第 3 步已标注为「**可选，仅本地跑评测/测试时需要**」。
> 手动迁移仅在需要单独控制迁移、或 backend 未启动时预建表时使用。
| 4 | `bash scripts/smoke_test.sh` | 见下方原文；**通过** | ✅ |
| 5 | `python -m evals.run_eval`（可选） | 报告路径在 README 已写明 | ✅ |

**第 4 步原始输出**：

```
[未鉴权 documents -> 401] 401
[不存在的路径（未鉴权）-> 401] 401
[端到端链路]
  token ok
  已上传 5 篇
  5 篇摄取 completed
  提问 -> answer_type=answered, citations=2
  端到端链路 ok
== 冒烟测试通过 ==
```

### 本次体检修出的问题

| # | 问题 | 处置 |
|---|---|---|
| 1 | 已知限制段写 kb_qa P95 = 7.8s，指标表写 6.6s —— **读者会以为矛盾** | README 该条改为**同时列出两个数并解释差异来源**（全量 35 题的 kb_qa 组 vs 同 10 题单跑；差异是单次采样的模型时延波动） |

> **无其它卡点**：README 的 `make` 缺失已给平替（`<details>` 折叠）、`smoke_test.sh` 已明确标注"用 bash 跑"、
> Windows 路径已给、Linux/macOS 路径已给、中文 HTTP 用 httpx 的警告已在。

---

## 视角 ②：面试官只看 README 前 30 行 + GIF

**三分钟体验路径**：`README 第 1 屏 → demo.gif → 评测数字 → 快速开始`。

| 面试官会问的 | README 里的回应位置 | 是否有据 |
|---|---|---|
| "这是什么？" | 第 3 行一句话定位 | ✅ |
| "架构什么样？" | 第 7–35 行 ASCII 架构图（Go / LangGraph 5 节点 / pgvector） | ✅ |
| "有什么技术含量？" | §三个技术决策（混合节点+预算防幻觉 / 两阶段发布防竞态 / 评测七指标可复算） | ✅ |
| "跑起来什么样？" | `docs/demo/demo.gif`（2.6MB 真录） | ✅ |
| "数字可信吗？" | §评测数字 + **每个数字的出处表**（逐项指向报告文件） | ✅ |
| "有没有吹牛？" | ⭐ 支持率 1.000 的读法（明写**残留未根治**）+ §已知限制 | ✅ |
| "能跑起来吗？" | §快速开始（含 make 平替、Windows/Linux 双路径） | ✅ 陌生人已实测 |

> **面试官视角的关键加分点**：README **主动暴露未达标与残留**（延迟余量小、引用越界未根治），
> 而不是只报满分。配合"每个数字的出处"表，**可当场抽查**。

---

## 三分钟体验的完整链路（陌生人+面试官合并）

```
README 第 1 屏（定位 + 架构图 + 3 技术决策）
   ↓ 30s
GIF（上传 → 引用 → 工具排查 → 拒答 → 澄清 → 轨迹）        ← docs/demo/demo.gif
   ↓ 60s
评测数字 + 出处表 + 「1.000 要会读」+ 已知限制              ← 可当场抽查报告
   ↓ 90s
clone → cp .env → docker compose up -d --build（14s）
   ↓ → 迁移 → bash scripts/smoke_test.sh → 通过
三分钟内可复现"看得见 + 跑得起来"
```

---

## 门禁（原文，两种口径）

```bash
# Go
cd backend && go vet ./... && go test ./...
# → vet exit 0；四个包全 ok：chat / handlers / ingest / test

# pytest —— 口径 A：无栈基线
cd ai-service && .venv/Scripts/python.exe -m pytest -q
# → 210 passed, 11 skipped

# pytest —— 口径 B：全栈环境
POSTGRES_HOST=localhost POSTGRES_PORT=55432 POSTGRES_USER=ops_pilot \
POSTGRES_PASSWORD=<见 deploy/.env> POSTGRES_DB=ops_pilot \
.venv/Scripts/python.exe -m pytest -q
# → 221 passed
```

> 数字均取自命令原始输出，**未做加法换算**。