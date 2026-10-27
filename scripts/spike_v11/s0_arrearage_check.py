import json, sys
from pathlib import Path
sys.path.insert(0, ".")
env = {}
for line in Path("../deploy/.env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")
import httpx
for m in ["qwen-flash", "qwen-plus", "text-embedding-v4"]:
    try:
        if "embedding" in m:
            with httpx.Client(timeout=30) as c:
                r = c.post(f"{env['BAILIAN_BASE_URL'].rstrip('/').replace('/compatible-mode/v1','')}/compatible-mode/v1/embeddings",
                           headers={"Authorization": f"Bearer {env['DASHSCOPE_API_KEY']}"},
                           json={"model": m, "input": "测试"})
            print(f"{m:20s} → {r.status_code} {r.text[:120]}")
        else:
            with httpx.Client(timeout=30) as c:
                r = c.post(f"{env['BAILIAN_BASE_URL'].rstrip('/')}/chat/completions",
                           headers={"Authorization": f"Bearer {env['DASHSCOPE_API_KEY']}"},
                           json={"model": m, "messages":[{"role":"user","content":"hi"}], "max_tokens":5})
            print(f"{m:20s} → {r.status_code} {r.text[:120]}")
    except Exception as e:
        print(f"{m:20s} → EXC {e}")
