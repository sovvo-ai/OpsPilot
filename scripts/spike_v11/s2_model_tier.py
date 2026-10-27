"""SPIKE-2（修正）：从 deploy/.env 直接读 key，不依赖 shell 环境。"""
import json, os, re, sys, time
from pathlib import Path
sys.path.insert(0, ".")

# 从 deploy/.env 解析（不 export，不打印值）
ENV_PATH = Path("../deploy/.env")
env = {}
for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    env[k.strip()] = v.strip().strip('"').strip("'")

KEY = env.get("DASHSCOPE_API_KEY", "")
BASE = env.get("BAILIAN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
print(f"key_len={len(KEY)}  base={BASE}")
assert len(KEY) > 20, "API Key 未读到"

CRITIC_PROMPT = """你是引用审查者。判断【结论】是否被你引用的【片段原文】支持。

只输出 JSON，不要任何其他文字：
{{"verdict": "supported|unsupported|needs_downgrade_label", "reason": "≤40字理由"}}

判定标准：
- supported：结论的事实要素能从片段原文直接读出（允许同义改写，如"存活探针"↔livenessProbe）
- unsupported：片段主题与结论对象无关，或结论核心事实在片段中不存在
- needs_downgrade_label：结论主体有依据，但附带片段未给出的具体参数/数值/机制

【结论】{claim}

【片段原文】（来自《{doc}》）
{source}

【输出】"""

CASES = [
    ("启动慢的应用应改用 startupProbe 提供启动宽限期", "k8s-pod-troubleshooting",
     "常见错误是把耗时的启动检查放进 livenessProbe，导致应用还没启动完就被反复重启。启动慢的应用应使用 startupProbe 保护。",
     "supported"),
    ("livenessProbe 失败会导致容器重启", "linux-network-troubleshooting",
     "用 ip link 查看网卡状态，state DOWN 说明链路层未就绪。ip route 确认默认网关是否存在。",
     "unsupported"),
    ("initialDelaySeconds 建议设置为大于应用启动耗时", "k8s-pod-troubleshooting",
     "启动慢的应用应使用 startupProbe 保护。", "needs_downgrade_label"),
]
VALID = {"supported", "unsupported", "needs_downgrade_label"}

def call(model, prompt):
    import httpx
    t0 = time.monotonic()
    with httpx.Client(timeout=40) as c:
        r = c.post(f"{BASE.rstrip('/')}/chat/completions",
                   headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                   json={"model": model, "messages": [{"role":"user","content":prompt}],
                         "temperature": 0.0, "max_tokens": 200,
                         "response_format": {"type": "json_object"}})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"], int((time.monotonic()-t0)*1000)

def parse(txt):
    s = (txt or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s); s = re.sub(r"\s*```$", "", s)
    try:
        d = json.loads(s); v = d.get("verdict","")
        return (v in VALID), v, d.get("reason","")
    except Exception as e:
        return False, "", f"parse_err:{e}"

results = {}
for model in ["qwen-flash", "qwen-plus"]:
    print(f"\n{'='*62}\n模型: {model}\n{'='*62}")
    ok = tot = 0; lat = []; correct = 0
    for run in range(5):
        for claim, doc, src, expect in CASES:
            try:
                txt, ms = call(model, CRITIC_PROMPT.format(claim=claim, doc=doc, source=src))
            except Exception as e:
                print(f"  run{run} FAIL {e}"); tot += 1; continue
            valid, v, reason = parse(txt); tot += 1; lat.append(ms)
            if valid: ok += 1
            if valid and v == expect: correct += 1
            if run == 0:
                mark = "OK  " if v == expect else "MISS"
                print(f"  [{mark}] {v:22s} {ms:5d}ms  {claim[:24]}…")
                if not valid: print(f"          raw={txt[:100]}")
    lat.sort()
    results[model] = dict(ok=ok, tot=tot, correct=correct, lat=lat)
    print(f"\n  JSON合法 {ok}/{tot} = {ok/tot:.1%}   三态判定正确 {correct}/{tot} = {correct/tot:.1%}")
    print(f"  耗时 min={lat[0]} median={lat[len(lat)//2]} max={lat[-1]} n={len(lat)}")

print(f"\n{'='*62}\n【对比】\n{'='*62}")
for m, r in results.items():
    print(f"  {m:12s} JSON={r['ok']}/{r['tot']}  正确={r['correct']}/{r['tot']}  median={r['lat'][len(r['lat'])//2]}ms  max={r['lat'][-1]}ms")
