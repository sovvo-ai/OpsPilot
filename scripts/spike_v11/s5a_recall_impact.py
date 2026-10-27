"""主题过滤器对 Recall@5 / MRR@5 的影响（35 题主集）。"""
import os, sys, json
sys.path.insert(0, ".")
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
print(f"题数: {len(ds)}")

def hit(chunks, gold_texts):
    """按 gold_chunk_texts 子串判断命中（与 run_eval 同口径）。"""
    for i, c in enumerate(chunks):
        body = c["content"]
        for g in gold_texts:
            if g[:20] in body or body[:40] in g:
                return i
    return -1

rows=[]
for d in ds:
    q = d["question"]
    gold = d.get("gold_chunk_texts") or []
    if not gold: continue
    try:
        base, _ = retrieve("demo-user", q, CFG, probe=True)
    except Exception as e:
        print("ERR", d["id"], e); continue
    flt, _ = filter_by_topic(base, q)
    ib, ifl = hit(base, gold), hit(flt, gold)
    rows.append({"id":d["id"],"n_base":len(base),"n_flt":len(flt),
                 "hit_base":ib>=0,"hit_flt":ifl>=0,
                 "mrr_base":(1/(ib+1)) if ib>=0 else 0,
                 "mrr_flt":(1/(ifl+1)) if ifl>=0 else 0})

n=len(rows)
rb=sum(r["hit_base"] for r in rows)/n
rf=sum(r["hit_flt"] for r in rows)/n
mb=sum(r["mrr_base"] for r in rows)/n
mf=sum(r["mrr_flt"] for r in rows)/n
print(f"\n{'指标':12s} {'baseline':>10s} {'filter':>10s} {'差':>10s}")
print(f"{'Recall@5':12s} {rb:10.4f} {rf:10.4f} {rf-rb:+10.4f}")
print(f"{'MRR@5':12s} {mb:10.4f} {mf:10.4f} {mf-mb:+10.4f}")
print(f"\n检索集大小: base均值={sum(r['n_base'] for r in rows)/n:.2f}  filter均值={sum(r['n_flt'] for r in rows)/n:.2f}")

worse=[r for r in rows if r["hit_base"] and not r["hit_flt"]]
better=[r for r in rows if not r["hit_base"] and r["hit_flt"]]
print(f"\nfilter 后**丢失**命中: {len(worse)} 题 → {[r['id'] for r in worse]}")
print(f"filter 后**新增**命中: {len(better)} 题 → {[r['id'] for r in better]}")
Path("../docs/testing/s5_recall_impact.json").write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding="utf-8")
