# V1.1 SPIKE 脚本（critic vs topic filter）

对应报告：
- `docs/testing/spike-report-v11-critic.md`（首轮，含阻塞）
- `docs/testing/spike-report-v11-critic-phase2.md`（三臂对照 + 主药建议）

| 脚本 | 验证项 | 网络/API |
|---|---|---|
| `s0_arrearage_check.py` | 账号可用性（欠费诊断） | ✅ |
| `s0b_route_check.py` | **m001 型问题走哪条路由** | ❌ |
| `s1_langgraph_critic.py` | LangGraph 独立节点 + 条件边 | ❌ |
| `s1b_prove_b_arm.py` | **证明 kb_qa 无 draft → critic 结构无效** | ✅ |
| `s2_model_tier.py` | qwen-flash vs qwen-plus（JSON mode） | ✅ |
| `s3_budget.py` | 预算 + 超时墙钟（含 53000/59000 勘误） | ❌ |
| `s4_adapter.py` | detector → critic 输入对接 | ❌ |
| `s5_three_arms.py` | **三臂对照（A/C）** | ✅ |
| `s5a_recall_impact.py` | 过滤器对 Recall/MRR 的影响 | ✅ |
| `s5b_threshold_scan.py` | **阈值扫描（0.0~3.0）** | ✅ |
| `s5c_diag_loss.py` | 误杀题诊断 | ✅ |
| `s6_concurrency_429.py` | 并发上限与 429 形态 | ✅ |

## 运行

```bash
cd ai-service
../.venv/Scripts/python.exe ../scripts/spike_v11/s3_budget.py        # 纯计算
../.venv/Scripts/python.exe ../scripts/spike_v11/s5_three_arms.py --runs 5
```

> 需要 API 的脚本从 `deploy/.env` 读 key（**shell env 里没有**，
> 直接读文件；否则报 `Illegal header value b'Bearer '`）。

## 关键结论（2026-10-27）

- **critic 覆盖不到缺陷路径**：m001 型问题全走 `kb_qa`，该路径无 `draft_answer`
- **filter 有效**：越界 21→1（−95%），Recall ±0，MRR +0.0975，零 LLM
- **阈值 1.0 最优**：MRR 峰值，1.5 起误杀 `q029`
- **超时墙钟**：6 节点 59000ms，加 critic 必破 60000（首版脚本漏算 `T_RETRIEVAL` 6000）
- **新账号并发**：40 全过，60 起 429，**仍无 `Retry-After`**
