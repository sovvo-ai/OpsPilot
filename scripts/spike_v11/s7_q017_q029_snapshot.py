"""生成 q017 / q029 的**真实检索快照**，供哨兵测试使用。

教训：凭记忆手写 chunk 内容会造出与语料不符的假分数（首版把 q029 写成 1.0~1.5，
实测是恰好 1.00）。**哨兵必须用真数据**，否则测的是想象。
"""
import json, os, sys
sys.path.insert(0, "ai-service")
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
env = {}
for line in (ROOT / "deploy/.env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")
os.environ.update(env)
sys.path.insert(0, str(ROOT / "ai-service"))
from app.config import get_config
from app.retrieval import retrieve
from app.topic_filter import extract_anchors, score_topic_consistency

CFG = get_config()
ds = {json.loads(l)["id"]: json.loads(l)
      for l in (ROOT / "ai-service/evals/datasets/qa_golden.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()}

out = {}
for qid in ["q017", "q029"]:
    d = ds[qid]; q = d["question"]
    anchors = extract_anchors(q)
    chunks, _ = retrieve("demo-user", q, CFG, probe=True)
    rows = []
    for c in chunks:
        sc = score_topic_consistency(c, anchors)
        is_gold = any(g[:20] in c["content"] or c["content"][:40] in g
                      for g in d.get("gold_chunk_texts") or [])
        rows.append({
            "doc_title": c["doc_title"], "score": c["score"],
            "topic_score": sc["score"], "is_gold": is_gold,
            "heading_path": c.get("heading_path", ""),
            "content": c["content"],
        })
    out[qid] = {"question": q, "anchors": anchors, "chunks": rows,
                "gold_doc_ids": d["gold_doc_ids"]}
    print(f"\n{qid}: anchors={anchors}")
    for r in rows:
        print(f"  topic={r['topic_score']:5.2f} retr={r['score']:.4f} "
              f"{'★GOLD' if r['is_gold'] else '     '} {r['doc_title']}")

p = ROOT / "docs/testing/q017_q029_snapshot.json"
p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n快照 → {p}")
