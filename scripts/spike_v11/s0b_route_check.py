"""验证 m001 型问题走哪条路由（事实核查，不采信口头）。"""
import sys
sys.path.insert(0, ".")
from app.graph.router import router_node
from app.graph.budget import Budget

cases = [
    ("m001 原题（有 history）", "那它怎么配？", [
        {"role":"user","content":"Pod 一直重启，怀疑是探针配置有问题，怎么查？"},
        {"role":"assistant","content":"先看 describe 的 Last State 与 Events，确认是 liveness 失败还是应用自身退出。"},
    ]),
    ("m001 对照单轮", "Kubernetes 的 livenessProbe 和 readinessProbe 应该怎么配置？", []),
    ("v3 复现的探针问题（无日志特征）", "Pod 探针应该怎么配？", []),
    ("真 troubleshoot（有 error 关键词）", "容器报 Error: CrashLoopBackOff 怎么排查？", []),
]

for label, q, hist in cases:
    st = {"question": q, "history": hist, "turn_index": 1 if hist else 0, "route_conf": 0}
    out = router_node(st, budget=Budget(), llm_call=None)
    print(f"{label:32s} → route={out['route']:16s} conf={out.get('route_conf')}")
