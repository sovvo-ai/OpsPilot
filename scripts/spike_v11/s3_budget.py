"""SPIKE-3：预算与超时的完整重算。

**勘误记录（2026-10-27）**：本脚本的**首版**只统计了 5 个「会调 LLM 的节点」，
漏掉 `retrieval`（它不调 LLM，但**有超时 T_RETRIEVAL=6000ms**）→ 得出 53000ms。
而 `agent-workflow.md:205` 与 `00-environment-and-plan.md:327` 都写 59000ms（含 retrieval）。

**定谳：文档正确，本脚本首版错误。** 现版本已补全 6 节点，并在输出中显式对比两种口径，
避免再次误读。

注意两个**不同**的约束（首版也混在了一起）：
  - LLM 额度：只数"会调 LLM 的节点" → 与 retrieval 无关
  - 超时总和：**所有节点**都算 → 必须含 retrieval
"""

MAX_LLM_CALLS = 6
RESERVED_FOR_FINAL = 1
WALL_CLOCK_MS = 60_000

# 全部 6 个节点的超时（权威：agent-workflow.md §5.1 / 计划书 §6.3）
T = {
    "router": 8000,
    "rewrite": 5000,
    "retrieval": 6000,
    "troubleshoot": 15000,
    "checker": 10000,
    "final": 15000,
    "critic": 0,  # 基线 0 = 未启用
}

# 会调 LLM 的节点（用于额度核算；retrieval 不在内）
LLM_NODES = {"router", "rewrite", "troubleshoot", "checker", "final", "critic"}


def report(title: str, path: list[tuple[str, int]]) -> dict:
    """path = [(节点, LLM调用次数)]；超时按节点计一次（与调用次数无关）。"""
    llm = sum(v for k, v in path if k in LLM_NODES)
    # ⚠️ 超时按**节点**算，不按调用次数乘 —— 同一节点重试共享其超时预算
    timeout = sum(T[k] for k, _v in path)
    over_n = max(0, llm - MAX_LLM_CALLS)
    over_t = max(0, timeout - WALL_CLOCK_MS)
    print(f"\n{title}")
    print("  路径: " + " → ".join(f"{k}×{v}" if v > 1 else k for k, v in path))
    print(f"  LLM={llm}/{MAX_LLM_CALLS} (超{over_n})   超时和={timeout}ms (墙钟超{over_t}ms)")
    return {"llm": llm, "timeout": timeout, "over_n": over_n, "over_t": over_t}


print("=" * 74)
print("【勘误对比】为什么首版算出 53000 而不是 59000")
print("=" * 74)
t_five = sum(T[k] for k in ["router", "rewrite", "troubleshoot", "checker", "final"])
t_six = sum(T[k] for k in ["router", "rewrite", "troubleshoot", "checker", "final", "retrieval"])
print(f"  5 个 LLM 节点:        {t_five}ms   ← 首版口径（错，漏 retrieval）")
print(f"  6 个全部节点:         {t_six}ms   ← 文档口径（agent-workflow:205）")
print(f"  差值 = {t_six - t_five}ms = T_RETRIEVAL {T['retrieval']}ms")
print(f"  → 定谳：文档正确，首版脚本错误（已修）")

print("\n" + "=" * 74)
print("【troubleshoot 路径】")
print("=" * 74)
base_path = [("router", 1), ("rewrite", 1), ("retrieval", 1), ("troubleshoot", 1), ("checker", 1), ("final", 1)]
report("① 现状（实测 1 次诊断）", base_path)

print("\n" + "=" * 74)
print("【加 critic 的两种排位 —— 超时都一样，额度语义不同】")
print("=" * 74)
T["critic"] = 6000
report("② critic 在 final 之后", base_path + [("critic", 1)])
report("③ critic 在 final 之前", [("router",1),("rewrite",1),("retrieval",1),("troubleshoot",1),("checker",1),("critic",1),("final",1)])

print("\n" + "=" * 74)
print("【reserved_for_final 不变量】")
print("=" * 74)
avail = MAX_LLM_CALLS - RESERVED_FOR_FINAL
print(f"  非 final 可用额度 = {MAX_LLM_CALLS} - {RESERVED_FOR_FINAL} = {avail} 次")
print(f"  非 final 实际需要 = router+rewrite+troubleshoot+checker+critic = 5 次")
print(f"  → {avail} >= 5 ? {'✅ 恰好够（零余量）' if avail >= 5 else '❌ 不够'}")
print(f"  若 critic 排在 final 之后：final 已用预留 1 次，critic 拿不到额度 → 必然被抢占")

print("\n" + "=" * 74)
print("【超时重分配候选（必须 ≤ 60000ms）】")
print("=" * 74)
cands = [
    ("现状 + critic 6000（不调整）", 6000, {}),
    ("BUD-1a: critic 1000", 1000, {}),
    ("BUD-1b: critic 3000 + rewrite 3000", 3000, {"rewrite": 3000}),
    ("BUD-1c: critic 6000 + rewrite 3000 + checker 8000", 6000, {"rewrite": 3000, "checker": 8000}),
    ("BUD-1d: critic 6000 + rewrite 3000 + checker 8000 + troubleshoot 12000", 6000,
     {"rewrite": 3000, "checker": 8000, "troubleshoot": 12000}),
]
for name, t_critic, override in cands:
    tt = dict(T); tt["critic"] = t_critic; tt.update(override)
    total = sum(tt[k] for k in ["router","rewrite","retrieval","troubleshoot","checker","critic","final"])
    ok = "✅" if total <= WALL_CLOCK_MS else "❌"
    detail = f"critic={t_critic}" + (f" {override}" if override else "")
    print(f"  {ok} {name:62s} 总计={total}ms  ({detail})")

print("\n" + "=" * 74)
print("【未来：若补上计划书的「第 2 次诊断」】")
print("=" * 74)
report("④ 2 次诊断 + critic", [("router",1),("rewrite",1),("retrieval",1),("troubleshoot",2),("checker",1),("critic",1),("final",1)])
print("  ⚠️ 当前 troubleshoot 只有 1 处 llm_call（troubleshoot.py:308），")
print("     第 2 次从未实现 —— 若补上，额度与超时双双爆掉。")

print("\n" + "=" * 74)
print("【filter 方案：零 LLM、零超时增量】")
print("=" * 74)
print("  topic_filter 是纯 Python 计算（字符串匹配），不进图、不调模型。")
print("  → LLM 额度：不变（+0）")
print("  → 超时总和：不变（+0ms）")
print("  → 这是它相对 critic 的结构性优势（不占任何预算）")