"""统一的 LLM 调用封装（供 graph 各节点注入）。

把"模型选择 + 超时 + 解析"集中到一处，节点只关心业务语义。
`make_llm_call(cfg)` 返回 `fn(role: str, prompt: str) -> str`。
"""

from __future__ import annotations

import logging

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
    """构造 LLM 调用函数。"""

    def llm_call(role: str, prompt: str) -> str:
        model = cfg.get(ROLE_MODEL_KEY.get(role, "llm_model_final"), cfg["llm_model_qa"])
        url = cfg["bailian_base_url"].rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {cfg['dashscope_api_key']}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0 if role in ("router", "checker", "rewrite") else 0.2,
            "max_tokens": 256 if role in ("router", "checker", "rewrite") else cfg.get("answer_max_tokens", 1024),
            "stream": False,
        }
        try:
            with _build_client(float(cfg.get("llm_timeout_seconds", 30))) as client:
                resp = client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        except Exception as exc:  # noqa: BLE001
            raise ChatError("MODEL_UNAVAILABLE", f"{role} 调用失败: {exc}") from exc

    return llm_call