"""SPIKE-4：detect_citation_overreach 输出 → critic 输入格式的对接成本实测。"""
import json, sys
sys.path.insert(0, ".")
from app.chat import detect_citation_overreach, _SENTENCE_SPLIT

# 构造 v3 实测的典型越界样本（m001 病灶）
answer = (
    "Pod 一直重启通常是探针配置问题。\n"
    "启动慢的应用应改用 startupProbe 提供启动宽限期[1]。\n"
    "另外 initialDelaySeconds 建议设置为大于应用启动耗时[2]。\n"
    "livenessProbe 失败会导致容器重启[2]。"
)
chunks = [
    {"chunk_id": "c-k8s", "doc_id": "d1", "doc_title": "k8s-pod-troubleshooting",
     "score": 0.78, "content": "常见错误是把耗时的启动检查放进 livenessProbe，导致应用还没启动完就被反复重启。启动慢的应用应使用 startupProbe 保护。"},
    {"chunk_id": "c-net", "doc_id": "d2", "doc_title": "linux-network-troubleshooting",
     "score": 0.51, "content": "用 ip link 查看网卡状态，state DOWN 说明链路层未就绪。ip route 确认默认网关。"},
]
citations = [
    {"index": 1, "chunk_id": "c-k8s", "doc_title": "k8s-pod-troubleshooting", "score": 0.78, "snippet": chunks[0]["content"], "source_type":"kb"},
    {"index": 2, "chunk_id": "c-net", "doc_title": "linux-network-troubleshooting", "score": 0.51, "snippet": chunks[1]["content"], "source_type":"kb"},
]

print("=== ① detector 原始输出 ===")
ov = detect_citation_overreach(answer, citations, chunks)
print(json.dumps(ov, ensure_ascii=False, indent=2))

print("\n=== ② 切句结果（_SENTENCE_SPLIT）===")
sents = [s for s in _SENTENCE_SPLIT.split(answer) if s.strip()]
for i, s in enumerate(sents):
    print(f"  [{i}] {s.strip()}")

print("\n=== ③ 可直接构造 critic 输入？—— 检查缺什么 ===")
# critic 需要: claim(单句) + pairs(声明编号对应的片段完整原文)
by_index = {c["index"]: c for c in citations}
full = {c["chunk_id"]: c["content"] for c in chunks}
for i, s in enumerate(sents):
    marks = [int(m) for m in __import__("re").findall(r"[\[<](\d{1,2})[\]>]", s)]
    if not marks:
        continue
    pairs = [{"index": m, "doc_title": by_index[m]["doc_title"],
              "source": full[by_index[m]["chunk_id"]]} for m in marks if m in by_index]
    print(f"  claim[{i}]: {s.strip()[:40]}...")
    print(f"    marks={marks}  → pairs可构造={len(pairs)>0}  pairs数={len(pairs)}")
