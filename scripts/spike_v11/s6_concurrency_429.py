"""新账号 429 形态复测（旧账号实测≈30 并发，无 Retry-After）。"""
import os, sys, time, json
sys.path.insert(0, ".")
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
env={}
for line in Path("../deploy/.env").read_text(encoding="utf-8").splitlines():
    line=line.strip()
    if line and not line.startswith("#") and "=" in line:
        k,v=line.split("=",1); env[k.strip()]=v.strip().strip('"').strip("'")
import httpx
KEY=env["DASHSCOPE_API_KEY"]; BASE=env["BAILIAN_BASE_URL"].rstrip("/")

def one(i):
    t0=time.monotonic()
    try:
        with httpx.Client(timeout=30) as c:
            r=c.post(f"{BASE}/embeddings",
                headers={"Authorization":f"Bearer {KEY}","Content-Type":"application/json"},
                json={"model":"text-embedding-v4","input":f"并发测试第{i}条"})
        return {"i":i,"code":r.status_code,"ra":r.headers.get("Retry-After"),
                "body":r.text[:150] if r.status_code!=200 else "", "ms":int((time.monotonic()-t0)*1000)}
    except Exception as e:
        return {"i":i,"code":"EXC","ra":None,"body":str(e)[:150],"ms":int((time.monotonic()-t0)*1000)}

for conc in [40, 60]:
    print(f"\n{'='*66}\n并发 {conc}\n{'='*66}")
    with ThreadPoolExecutor(max_workers=conc) as ex:
        rs=list(ex.map(one, range(conc)))
    ok=sum(1 for r in rs if r["code"]==200)
    c429=[r for r in rs if r["code"]==429]
    other=[r for r in rs if r["code"] not in (200,429)]
    print(f"  200={ok}/{conc}  429={len(c429)}  其他={len(other)}")
    if c429:
        ras={r["ra"] for r in c429}
        print(f"  Retry-After 值集合: {ras}")
        print(f"  429 body 样例: {c429[0]['body'][:140]}")
    if other:
        print(f"  其他错误样例: {other[0]['code']} {other[0]['body'][:120]}")
