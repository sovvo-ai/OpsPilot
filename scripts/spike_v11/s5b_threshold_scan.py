"""扫 min_consistency 阈值，看 Recall@5 / MRR@5 / 检索集大小的曲线。"""
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
from app.topic_filter import filter_by_topic
CFG=get_config()

ds=[json.loads(l) for l in Path("evals/datasets/qa_golden.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
def hit(chunks, gold):
    for i,c in enumerate(chunks):
        for g in gold:
            if g[:20] in c["content"] or c["content"][:40] in g: return i
    return -1

# 缓存检索结果，避免重复调用
cache={}
for d in ds:
    if not d.get("gold_chunk_texts"): continue
    try: cache[d["id"]]=(d["question"], retrieve("demo-user",d["question"],CFG,probe=True)[0])
    except Exception as e: print("ERR",d["id"],e)

print(f"{'阈值':>8s} {'Recall@5':>10s} {'MRR@5':>10s} {'集大小':>8s} {'保留率':>8s} {'丢失题':>6s}")
print("-"*60)
for th in [0.0, 0.3, 0.6, 1.0, 1.5, 2.0, 3.0]:
    n=0; rb=mr=0.0; nk=0; nb=0; lost=[]
    for qid,(q,base) in cache.items():
        gold = next(d for d in ds if d["id"]==qid).get("gold_chunk_texts") or []
        flt,_ = filter_by_topic(base,q,min_consistency=th)
        ib = hit(base,gold); if_ = hit(flt,gold)
        n+=1; nk+=len(flt); nb+=len(base)
        if ib>=0: rb+=1
        if if_>=0: mr+=1/(if_+1)
        if ib>=0 and if_<0: lost.append(qid)
    print(f"{th:8.1f} {rb/n:10.4f} {mr/n:10.4f} {nk/n:8.2f} {nk/nb:8.1%} {len(lost):6d}")
    if lost: print(f"         丢失: {lost}")
