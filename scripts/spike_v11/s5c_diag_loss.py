import os, sys, json
sys.path.insert(0,".")
from pathlib import Path
env={}
for line in Path("../deploy/.env").read_text(encoding="utf-8").splitlines():
    line=line.strip()
    if line and not line.startswith("#") and "=" in line:
        k,v=line.split("=",1); env[k.strip()]=v.strip().strip('"').strip("'")
os.environ.update(env)
from app.config import get_config
from app.retrieval import retrieve
from app.topic_filter import extract_anchors, _specific_anchors, score_topic_consistency
CFG=get_config()
ds={d["id"]:d for d in [json.loads(l) for l in Path("evals/datasets/qa_golden.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]}

for qid in ["q002","q007","q011","q012","q013"]:
    d=ds[qid]; q=d["question"]
    print(f"\n{'='*72}\n{qid}: {q}")
    print(f"  gold: {[g[:40] for g in (d.get('gold_chunk_texts') or [])][:2]}")
    anchors=extract_anchors(q); spec=_specific_anchors(anchors)
    print(f"  锚点(全部): {anchors}")
    print(f"  锚点(特定): {spec}")
    chunks,_=retrieve("demo-user",q,CFG,probe=True)
    for i,c in enumerate(chunks,1):
        sc=score_topic_consistency(c,anchors)
        is_gold = any(g[:20] in c["content"] or c["content"][:40] in g for g in (d.get("gold_chunk_texts") or []))
        mark = "★GOLD" if is_gold else "     "
        print(f"   [{i}] {c['score']:.4f} topic={sc['score']:.1f} {mark} {c['doc_title']:28s} matched={sc['matched'][:3]}")
