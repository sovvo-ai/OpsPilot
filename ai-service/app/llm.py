"""统一的 LLM 调用封装（供 graph 各节点注入）。

把"模型选择 + 超时 + 解析"集中到一处，节点只关心业务语义。
`make_llm_call(cfg)` 返回 `fn(role: str, prompt: str, on_delta=None) -> str`。

**S9 流式**：`fn` 新增可选参数 `on_delta(chunk: str) -> None`。
- `on_delta is None` → 非流式（**行为与 S8 完全一致**，`stream=False`）；
- `on_delta` 提供时 → 走 SSE 增量（`stream=True`），每个增量回调一次，返回值仍是**完整文本**。

**为什么同一函数而非第二套**：PRD §8.5-S5 要求"流式与非流式共用同一编排与落库逻辑，
不允许两套代码路径"。流式只是**同一调用的传输方式差异**，业务语义（role→model、
temperature、max_tokens）必须完全一致，故合并到一处。
"""

from __future__ import annotations

import json
import logging
from typing import Callable

from app.chat import ChatError
from app.ingest import _build_client

logger = logging.getLogger("app.llm")

# role → cfg 中的模型键
ROLE_MODEL_KEY = {
    "router": "llm_model_router",
    "qa": "llm_model_qa",
    "troubleshoot": "llm_model_troubleshoot",
    "checker": "llm_model_checker",
    "final": "llm_model_final",
    "rewrite": "llm_model_router",  # 改写用轻量模型
}


def make_llm_call(cfg: dict):
    """构造 LLM 调用函数。

    返回 `llm_call(role, prompt, on_delta=None) -> str`：
    - `on_delta=None`：非流式（与 S8 行为一致）
    - `on_delta=fn`：流式，逐块回调，返回值仍为完整文本
    """

    def llm_call(
        role: str,
        prompt: str,
        on_delta: Callable[[str], None] | None = None,
    ) -> str:
        model = cfg.get(ROLE_MODEL_KEY.get(role, "llm_model_final"), cfg["llm_model_qa"])
        url = cfg["bailian_base_url"].rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {cfg['dashscope_api_key']}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            # B2 修正：final 也降到 0，减少"漏标 `[n]`"的随机抖动
            # （answer_type 已按语义判定，不再依赖引用；但引用稳定性仍影响 A3b）
            "temperature": 0.0 if role in ("router", "checker", "rewrite", "final") else 0.2,
            "max_tokens": 256 if role in ("router", "checker", "rewrite") else cfg.get("answer_max_tokens", 1024),
            "stream": on_delta is not None,
        }
        timeout = float(cfg.get("llm_timeout_seconds", 30))
        try:
            if on_delta is None:
                with _build_client(timeout) as client:
                    resp = client.post(url, headers=headers, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                return data["choices"][0]["message"]["content"].strip()

            # 流式：逐块解析 SSE，边收边回调；返回值仍是完整文本
            return _stream_call(url, headers, payload, timeout, on_delta)
        except ChatError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ChatError("MODEL_UNAVAILABLE", f"{role} 调用失败: {exc}") from exc

    return llm_call


def _stream_call(url, headers, payload, timeout, on_delta: Callable[[str], None]) -> str:
    """执行流式调用；逐块回调 `on_delta`，返回拼接后的完整文本。

    上游为 OpenAI 兼容 SSE（百炼）：每行 `data: {...}`，结束为 `data: [DONE]`。
    **异常向上抛** —— 由调用方决定降级（不允许静默断流，PRD §8.5-S3）。
    """
    parts: list[str] = []
    with _build_client(timeout) as client:
        with client.stream("POST", url, headers=headers, json=payload) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    parts.append(delta)
                    on_delta(delta)
    return "".join(parts).strip()