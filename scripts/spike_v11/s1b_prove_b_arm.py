"""B 臂等价证明：kb_qa 路径不产生 draft_answer → critic 无输入可审。"""
import os, sys
sys.path.insert(0, ".")
from pathlib import Path
env = {}
for line in Path("../deploy/.env").read_text(encoding="utf-8").splitlines():
    line=line.strip()
    if line and not line.startswith("#") and "=" in line:
        k,v=line.split("=",1); env[k.strip()]=v.strip().strip('"').strip("'")
os.environ.update(env)

from app.config import get_config
from app.graph.runner import run_turn
from app.llm import make_llm_call
from app.retrieval import retrieve
CFG = get_config()

print("=== kb_qa 路径的 steps（看是否有 draft 产出）===")
res = run_turn(
    {"request_id":"proof","session_id":"s","turn_index":1,
     "question":"那它怎么配？",
     "history":[{"role":"user","content":"Pod 一直重启，怀疑是探针配置有问题，怎么查？"},
                {"role":"assistant","content":"先看 describe。"}],
     "owner_id":"demo-user"},
    llm_call=make_llm_call(CFG), retrieve_fn=retrieve)
print("route:", res["route"])
print("node_path:", res["node_path"])
for s in res["steps"]:
    print(f"  - {s['node']:14s} {s.get('output_summary','')}")
print()
print("关键：draft_answer 只在 troubleshoot.py 产生；kb_qa 的 node_path 无 troubleshoot")
print("→ critic（排在 final 前，审 draft）在该路径上无输入可审 = 结构上不可能生效")
