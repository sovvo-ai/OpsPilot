# 工具策略（V1）

| 项 | 值 |
|---|---|
| 版本 | v1.0 |
| 状态 | 已冻结（2026-10-22 S2 审查通过）。仅允许实测结果触发修改 |
| 上游 | `docs/prd/PRD.md` v1.7 §8、FR-23~FR-28 |
| 实现位置 | `ai-service/app/tools/` |

---

## 1. 封闭白名单

```python
TOOL_REGISTRY = {
    "analyze_error_log": analyze_error_log,
    "check_port_conflict": check_port_conflict,
    "search_knowledge_base": search_knowledge_base,
}
```

- 调用名不在集合内 → `400 TOOL_NOT_ALLOWED`，记审计，**不抛 500**。
- 注册表是**常量**：不允许运行时注册、不允许插件式扩展、不允许从配置读工具名。
- 三个工具都是**纯函数或只读查询**，不触达主机、不执行命令。

---

## 2. 工具契约

### 2.1 `analyze_error_log`

| 项 | 内容 |
|---|---|
| 入参 | `log_text`（string，1–8192 字符）、`language`（可选，`auto`/`zh`/`en`，默认 `auto`） |
| 出参 | `error_type`、`key_lines`（行号数组）、`timestamps`、`error_codes`、`groups`（按模式分组计数）、`truncated`（bool） |
| 实现 | 正则 + 规则匹配。**不调模型、不执行命令、不读文件** |
| 边界 | >8192 字符截断并 `truncated=true`；空字符串 → `422 INVALID_TOOL_ARGS` |
| 不做 | 不解释根因、不给结论（结论归 troubleshoot 节点） |

```json
// 出参示例
{"error_type":"connection_refused","key_lines":[1,3],"timestamps":["2026-10-22T12:00:03Z"],
 "error_codes":["ECONNREFUSED"],"groups":[{"pattern":"dial tcp","count":2}],"truncated":false}
```

### 2.2 `check_port_conflict`

| 项 | 内容 |
|---|---|
| 入参 | `ports`：1–20 项数组，每项 `{port:int, protocol:"tcp"\|"udp", service?:string, pid?:int}`；`expected_owner?:string` |
| 出参 | `conflicts[]`（`{port, protocol, conflict:bool, suspected_owner, reason}`）、`summary` |
| 实现 | 纯数据校验与比对。**不调用 `netstat`/`ss`/`lsof`** |
| 边界 | `port` 不在 1–65535 → `422 INVALID_TOOL_ARGS`；同端口不同协议不算冲突 |
| 说明 | V1 只分析"用户提供的模拟数据"，真实主机探测属 V2 |

### 2.3 `search_knowledge_base`

| 项 | 内容 |
|---|---|
| 入参 | `query`（string，1–500 字符）、`top_k`（int，1–10，默认 `RETRIEVAL_TOP_K`=5） |
| 出参 | `chunks[]`：`{chunk_id, doc_id, doc_title, score, content}` |
| 实现 | 复用 retrieval 的检索 SQL（含 `visible=TRUE` + `status='completed'` + `owner_id` 过滤） |
| 边界 | `top_k > 10` → 截断为 10 并标注；不得跨 owner |

> 复用而非另写：**工具与检索节点共用同一函数**，避免"工具能搜到的片段，主链路搜不到"这类不一致。

---

## 3. 硬性禁令（代码层强制）

| # | 禁令 | 强制方式 |
|---|---|---|
| P1 | 不存在通用执行器：无 `exec` / `subprocess` / `os.system` / `eval` / `compile` | CI 静态检查 `app/tools/` 下出现即失败 |
| P2 | 模型输出的字符串不得作为命令、SQL、路径使用 | 入参全部经 Pydantic 校验 + 枚举白名单 |
| P3 | 无路径类参数 | V1 三个工具都没有文件/目录参数 |
| P4 | 无 shell 拼接 | 不存在拼命令的代码路径 |
| P5 | 同参数不得重复调用 | 单轮内同一工具对同一规范化参数组合只执行一次，后续调用复用缓存结果（去重） |

> P1 的 CI 规则要**排除 `docs/`**（文档里存在 `subprocess` 等字样作为政策描述），与 `spike-report.md` §9 M1 的 `sk-` 扫描同一处理方式。

---

## 4. 执行限制

| 限制 | 值 | 超限行为 |
|---|---|---|
| 单工具超时 | 5s | 中止，`status=timeout`，回答注明"工具调用失败" |
| 单轮工具调用次数 | 3 | 第 4 次起直接跳过，`degraded=true`，`degrade_reasons+=["tool_skipped_budget"]` |
| 单次输出 | ≤4KB | 截断并标 `truncated=true` |
| 入参大小 | 见各工具 | 超限 `422 INVALID_TOOL_ARGS` |

---

## 5. 审计

每次调用写 `tool_calls` 一行：

```json
{"run_id":"…","step_index":3,"tool_name":"analyze_error_log",
 "args_digest":"sha256:ab12cd34","status":"ok","duration_ms":37}
```

- **只存摘要**，不存原始入参（日志可能含敏感信息；PRD NFR-S5）。
- `args_digest = sha256(canonical_json(args))[:32]`。
- `status ∈ {ok, timeout, error, rejected}`；`rejected` 用于白名单拒绝与参数校验失败。

---

## 6. 注入与滥用用例（对应 PRD A7；用例 S3 落地为 `ai-service/tests/unit/test_tool_policy.py` 与 `ai-service/tests/integration/test_tool_injection.py`）

| 用例 | 输入 | 期望 |
|---|---|---|
| 超长日志 | 20KB 文本 | 截断至 8KB，`truncated=true`，不报错 |
| 非法端口 | `port: 70000` | `422 INVALID_TOOL_ARGS` |
| SQL 片段注入 | `query: "'; DROP TABLE doc_chunks; --"` | 作为普通文本检索，无 SQL 执行；参数化查询不拼接 |
| 路径穿越 | `query: "../../etc/passwd"` | 当文本处理，不触达文件系统 |
| 命令注入 | `log_text: "$(rm -rf /)"` | 当字符串分析，不执行 |
| 非白名单工具 | 调用 `read_log_tail` | `400 TOOL_NOT_ALLOWED` + 审计 |

---

## 7. 与 V2 的边界

V1 三个工具全部**不触达主机**。V2 若要引入 `ss`/`journalctl` 等真实只读诊断，必须先补：沙箱（只读根文件系统）、命令白名单（枚举而非字符串匹配）、输出脱敏、以及一份独立的 ADR。**不得在 V1 代码里预留"通用命令执行"的口子。**
