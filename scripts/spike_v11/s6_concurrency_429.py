"""并发上限与 429 形态复测（新账号）。

**为什么要重测**：S1 spike-report 里"并发上限≈30、429 无 Retry-After"是**旧账号**的实测。
换账号后沿用旧数字 = 埋一个"应该可以"的隐患（ADR-0002 §6.1 同类问题）。

输出：纯净表格 + JSON（上一轮贴回的数字排版糊了，本次直接落结构化数据）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
env: dict[str, str] = {}
for line in (ROOT / "deploy" / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")

import httpx  # noqa: E402

KEY = env["DASHSCOPE_API_KEY"]
BASE = env["BAILIAN_BASE_URL"].rstrip("/")
LEVELS = [10, 20, 30, 40, 50, 60]


def one(i: int) -> dict:
    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(
                f"{BASE}/embeddings",
                headers={
                    "Authorization": f"Bearer {KEY}",
                    "Content-Type": "application/json",
                },
                json={"model": "text-embedding-v4", "input": f"concurrency probe {i}"},
            )
        return {
            "i": i,
            "code": r.status_code,
            "retry_after": r.headers.get("Retry-After"),
            "body": r.text[:200] if r.status_code != 200 else "",
            "ms": int((time.monotonic() - t0) * 1000),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "i": i,
            "code": "EXC",
            "retry_after": None,
            "body": str(exc)[:200],
            "ms": int((time.monotonic() - t0) * 1000),
        }


def main() -> None:
    rows = []
    print(f"{'并发':>6} | {'200':>8} | {'429':>5} | {'其他':>5} | {'Retry-After':>12} | 结论")
    print("-" * 78)
    for conc in LEVELS:
        with ThreadPoolExecutor(max_workers=conc) as ex:
            rs = list(ex.map(one, range(conc)))
        ok = sum(1 for r in rs if r["code"] == 200)
        c429 = [r for r in rs if r["code"] == 429]
        other = [r for r in rs if r["code"] not in (200, 429)]
        ra = sorted({str(r["retry_after"]) for r in c429}) if c429 else []
        verdict = "全通过" if not c429 and not other else "触发限流"
        print(
            f"{conc:>6} | {ok:>3}/{conc:<4} | {len(c429):>5} | {len(other):>5} | "
            f"{(','.join(ra) if ra else '—'):>12} | {verdict}"
        )
        rows.append({
            "concurrency": conc,
            "ok": ok,
            "n_429": len(c429),
            "n_other": len(other),
            "retry_after_values": ra,
            "sample_429_body": c429[0]["body"] if c429 else "",
            "sample_other": other[0]["body"] if other else "",
        })

    clean = [r["concurrency"] for r in rows if r["n_429"] == 0 and r["n_other"] == 0]
    throttled = [r["concurrency"] for r in rows if r["n_429"] > 0]
    upper_ok = max(clean) if clean else None
    first_fail = min(throttled) if throttled else None
    has_ra = any(
        r["retry_after_values"] and r["retry_after_values"] != ["None"] for r in rows
    )

    print()
    print("=" * 78)
    print("【结论】")
    print("=" * 78)
    print(f"  全通过的最高并发 = {upper_ok}")
    print(f"  首次触发 429 的并发 = {first_fail}")
    print(
        f"  Retry-After 有效值 = {has_ra}"
        + ("（全部为 None）" if not has_ra else "")
    )
    print(
        f"  → 并发上限约 {upper_ok}~{first_fail}；"
        + ("**仍无 Retry-After**，须自行指数退避" if not has_ra else "有 Retry-After")
    )

    out = {
        "tested_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "model": "text-embedding-v4",
        "levels": rows,
        "max_clean_concurrency": upper_ok,
        "first_throttled_concurrency": first_fail,
        "retry_after_available": has_ra,
        "old_s1_claim": "并发上限≈30（旧账号实测）",
        "new_conclusion": (
            f"并发上限约 {upper_ok}~{first_fail}"
            + ("；Retry-After 仍为 None" if not has_ra else "")
        ),
    }
    p = ROOT / "docs/testing/concurrency_429_recheck.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n原始数据 → {p}")


if __name__ == "__main__":
    main()