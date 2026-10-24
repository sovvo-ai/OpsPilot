# OpsPilot 运行手册（Runbook）

> 面向**接手运维的人**：如何起停、排障、以及本项目特有的坑。
> 所有命令在项目根目录执行（除注明 `cd deploy` 的）。

---

## 1. 起停

### 1.1 首次启动

```bash
cp deploy/.env.example deploy/.env
# 编辑 deploy/.env，至少填 DASHSCOPE_API_KEY

docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
python scripts/run_migrate.py up        # 迁移 0001~0003
bash scripts/smoke_test.sh              # 冒烟（含真实链路）
```

### 1.2 日常起停

```bash
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d
docker compose -f deploy/docker-compose.yml down          # 停（保留数据卷）
docker compose -f deploy/docker-compose.yml down -v       # ⚠️ 停并**清空数据卷**
```

> ⚠️ `down -v` 后**必须重跑迁移 + 重新摄取语料**，否则表不存在。
> 计时基线：干净环境到冒烟通过 **28.4s**（见 `docs/testing/s8-acceptance-report.md` §3）。

### 1.3 改了代码后重建

| 改了哪里 | 重建命令 |
|---|---|
| `ai-service/` | `docker compose build ai-service && docker compose up -d --force-recreate ai-service` |
| `backend/` | `docker compose build backend && docker compose up -d --force-recreate backend` |
| `backend/migrations/` | 重建 backend **并**跑 `python scripts/run_migrate.py up` |

---

## 2. 排障速查

### 2.1 健康检查

```bash
curl -s http://127.0.0.1:8081/health/live   # 进程存活（永远 200）
curl -s http://127.0.0.1:8081/health/ready  # 依赖就绪（含 ai-service / DB / 迁移版本）
```

`/health/live` 输出含 `build_time` 与 `version`（`-ldflags` 注入），可用来确认**当前跑的是哪次构建**。

### 2.2 常见现象

| 现象 | 原因 | 处置 |
|---|---|---|
| 提问返回 `503 MODEL_UNAVAILABLE` | ai-service 挂了 | `docker logs opspilot-ai`，必要时 `--force-recreate ai-service` |
| `/health/ready` 503，业务接口 503 `DB_UNAVAILABLE` | postgres 挂了 | `docker ps` 看 `opspilot-postgres` 是否 healthy |
| 迁移报"表不存在" | 执行过 `down -v` 但没重跑迁移 | `python scripts/run_migrate.py up` |
| 上传后一直 `pending` | dispatcher 未派发 / watchdog 未收敛 | 查 `ingest_jobs` 表；>150s 应由 watchdog 收敛为 `failed` |
| 上传返回 401 | 未带 token | 走 `/api/v1/auth/dev-token?user=demo-user` 取 token |
| 中文请求失败/乱码 | **用了 Windows shell `curl`** | 改用 Python `httpx`（见 §3.1） |

---

## 3. 本项目特有的坑（重要）

### 3.1 含中文的请求必须用 Python `httpx`，禁用 Windows shell `curl`

Windows shell 传中文参数会被转义/编码破坏 —— 同一请求在 `httpx` 下正常、在 `curl` 下失败。
**S7 轮次的审查者与实现者双双踩过**，已固化为 `test-plan` G7。

```python
import httpx
r = httpx.post("http://127.0.0.1:8081/api/v1/sessions/{sid}/messages",
               headers={"Authorization": f"Bearer {tok}"},
               json={"question": "Nginx 的 502 和 504 分别代表什么？"}, timeout=180)
```

### 3.2 Windows `subprocess` 传含换行的命令行参数会被截断

传 `["py", "script.py", "多行\n内容"]` 时，**脚本只收到第一行**。
→ 含换行的内容一律走**环境变量**（见 `scripts/demo_ask.py` 的 `DEMO_QUESTION` / `DEMO_LOG`）。

### 3.3 SSE 断连检测的 4 层坑（§8.5-S4）

改 `backend/internal/handlers/stream.go` 的断连逻辑前**必读**（同一内容见该文件包注释）：

1. **`c.Request.Context()` 取消滞后** —— SSE 客户端多为 TCP 半开，Go 需读到 FIN 才取消 ctx。
   → 需**心跳 goroutine** 主动探测写失败。
2. **心跳写小数据不报错** —— 响应几 KB 远小于内核发送缓冲（数十 KB）。
   → 心跳需带 **8KB 注释填充**（`:` 开头，SSE 规范中客户端忽略）。
3. **`streamErr` 形态** —— 断连表现为 `*HTTPError{Code:"STREAM_INTERRUPTED"}`，
   **不是** `context.Canceled`；两者都要判。
4. **分支顺序（最隐蔽）** —— 断连分支必须在 `streamErr` 处理**之后**，
   否则判定时 `clientGone` 未置位，**断连分支永不执行**。

**验证方式**：`scripts/s9_sse_contract.py`（全新栈 9/9 通过）。
测试侧注意：`httpx` 的 `break` **不保证关 TCP**，需**裸 socket + `SO_LINGER(0)` 强制 RST**。

**排障命令**：

```bash
# 看某个 run 是否被标记为断连
docker exec opspilot-postgres psql -U ops_pilot -d ops_pilot -c \
  "SELECT id, terminated_reason, llm_calls, duration_ms FROM agent_runs
    WHERE terminated_reason='interrupted' ORDER BY created_at DESC LIMIT 5;"
```

正常断连特征：`terminated_reason=interrupted`、**`llm_calls=0`**、`duration_ms` 很小。

### 3.4 本机 6379 端口被带密码的 Redis 占用

Docker 起的无密码 `redis:7` 映射 6379 会冲突（报 `AuthenticationError: HELLO ...`）。
→ 本项目的 Redis（如启用）映射到 **6380**，`REDIS_PORT` 用 6380。

### 3.5 Docker 构建代理坑

Docker Desktop 配了代理后，buildkit 会把 `HTTP_PROXY` 注入构建容器，
而容器内 `127.0.0.1` 指容器自己 → `ProxyError: Cannot connect to proxy`。
→ Dockerfile 的 `RUN` 里 **`unset` 代理变量**（已处理），并配国内源。

### 3.6 向量库路径不能含中文

`chromadb` / `hnswlib` 底层是 C++ 扩展，Windows 窄字符 API 无法处理中文路径。
→ 如启用本地向量库，`DB_DIR` 必须用纯英文路径。

---

## 4. 验收与评测

```bash
# 冒烟（健康 + 鉴权边界 + 上传 5 篇 + 提问）
bash scripts/smoke_test.sh

# A10 §9.4 异常矩阵（18 项）
ai-service/.venv/Scripts/python.exe scripts/a10_exception_matrix.py

# A11 干净环境计时
ai-service/.venv/Scripts/python.exe scripts/a11_clean_start_timing.py

# SSE 契约（§8.5 六条 + 断连）
ai-service/.venv/Scripts/python.exe scripts/s9_sse_contract.py

# 35 题评测（约 4 分钟）
cd ai-service && .venv/Scripts/python.exe -m evals.run_eval
```

结果落盘位置：评测报告 `ai-service/evals/reports/`，验收 JSON `.workbuddy/`。

---

## 5. 环境事实（装机时注意）

| 项 | 值 |
|---|---|
| Python | 3.11（`ai-service/.venv` 用系统 Python 3.11 建） |
| Go | 见 `backend/go.mod`（vendor 模式，容器内不联网） |
| DB 端口映射 | **55432** → 容器 5432（避让本机已有 postgres） |
| 后端端口 | 8081 |
| ai-service 端口 | 8000 |
| `make` | **本机未安装** —— 用 §1 的原始命令（README 已给平替） |