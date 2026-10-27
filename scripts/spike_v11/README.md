# V1.1 SPIKE 脚本（critic 审查者）

对应报告：`docs/testing/spike-report-v11-critic.md`

| 脚本 | 验证项 | 需要网络/API |
|---|---|---|
| `s0_arrearage_check.py` | 账号可用性探测（欠费诊断） | ✅ 是 |
| `s1_langgraph_critic.py` | LangGraph 独立节点 + 条件边落位 | ❌ 否 |
| `s2_model_tier.py` | qwen-flash vs qwen-plus 的 JSON mode 稳定性 | ✅ 是 |
| `s3_budget.py` | 预算 + 超时墙钟重算 | ❌ 否 |
| `s4_adapter.py` | detector → critic 输入对接 | ❌ 否 |

## 运行

```bash
cd ai-service
../.venv/Scripts/python.exe ../scripts/spike_v11/s1_langgraph_critic.py
../.venv/Scripts/python.exe ../scripts/spike_v11/s3_budget.py
```

> `s2` / `s0` 从 `deploy/.env` 读 key，**不依赖 shell 环境变量**
> （实测 WorkBuddy Bash 的 env 里没有 `DASHSCOPE_API_KEY`，只能读文件）。

## 当前状态（2026-10-27）

- s1 / s3 / s4：**已跑，通过**
- s2 / s0：**被阻塞** —— 百炼账号欠费（`code: "Arrearage"`），三个模型全 400
