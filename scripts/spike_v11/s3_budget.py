"""SPIKE-3（修正）：预算与超时的完整重算。"""
MAX_LLM_CALLS = 6
RESERVED_FOR_FINAL = 1
WALL_CLOCK_MS = 60_000
T = {"router": 8000, "rewrite": 5000, "retrieval": 6000,
     "troubleshoot": 15000, "checker": 10000, "final": 15000, "critic": 8000}

def report(title, path):
    n = sum(v for _, v in path)
    t = sum(T[k] * v for k, v in path)
    over_n = max(0, n - MAX_LLM_CALLS)
    over_t = max(0, t - WALL_CLOCK_MS)
    print(f"\n{title}")
    print("  " + " + ".join(f"{k}×{v}" if v > 1 else k for k, v in path))
    print(f"  LLM={n}/{MAX_LLM_CALLS} (超{over_n})   超时和={t}ms (墙钟超{over_t}ms)")
    return n, t, over_n, over_t

print("=" * 70)
print("【现状】troubleshoot 最坏路径（实测：1 次诊断）")
print("=" * 70)
report("① 现状", [("router",1),("rewrite",1),("troubleshoot",1),("checker",1),("final",1)])

print("\n" + "=" * 70)
print("【加 critic 的三种排位】")
print("=" * 70)
# 排位 A：critic 在 final 之后
report("② critic 在 final 之后（最直觉的排法）",
       [("router",1),("rewrite",1),("troubleshoot",1),("checker",1),("final",1),("critic",1)])
# 排位 B：critic 在 evidence 之前（final 之前）
report("③ critic 在 final 之前（final 仍保留预留额度）",
       [("router",1),("rewrite",1),("troubleshoot",1),("checker",1),("critic",1),("final",1)])

print("\n" + "=" * 70)
print("【关键约束分析】")
print("=" * 70)
print(f"  MAX_LLM_CALLS = {MAX_LLM_CALLS}, RESERVED_FOR_FINAL = {RESERVED_FOR_FINAL}")
print(f"  → 非 final 节点可用额度 = {MAX_LLM_CALLS - RESERVED_FOR_FINAL} 次")
print(f"  → 非 final 节点实际需要 = router+rewrite+troubleshoot+checker+critic = 5 次")
print(f"  → {MAX_LLM_CALLS - RESERVED_FOR_FINAL} >= 5 ? {'✅ 恰好够' if MAX_LLM_CALLS - RESERVED_FOR_FINAL >= 5 else '❌ 不够'}")
print(f"\n  超时约束（硬）：所有节点超时之和必须 ≤ {WALL_CLOCK_MS}ms，否则墙钟必然打断最后一个节点")
print(f"  → 现状 = {sum(T[k] for k in ['router','rewrite','troubleshoot','checker','final'])}ms（余 7000ms）")
print(f"  → 加 critic({T['critic']}ms) = {sum(T[k] for k in ['router','rewrite','troubleshoot','checker','final'])+T['critic']}ms ❌ 超 1000ms")

print("\n  可选的超时重分配（保住 ≤60000ms）:")
opts = [
    ("critic 只用 7000ms", {"critic": 7000}, 7000),
    ("critic 7000 + rewrite 降到 4000", {"critic": 7000, "rewrite": 4000}, 7000-1000),
    ("critic 6000 + checker 降到 8000", {"critic": 6000, "checker": 8000}, None),
]
base = {"router":8000,"rewrite":5000,"troubleshoot":15000,"checker":10000,"final":15000}
for name, override, _ in opts:
    t = dict(base); t.update(override); total = sum(t.values())
    print(f"    - {name:34s} → 总计 {total}ms {'✅' if total <= WALL_CLOCK_MS else '❌'}")

print("\n" + "=" * 70)
print("【未来：若补上第 2 次诊断】")
print("=" * 70)
report("④ 2次诊断 + critic", [("router",1),("rewrite",1),("troubleshoot",2),("checker",1),("critic",1),("final",1)])
