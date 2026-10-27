"""OpsPilot AI Service 配置。"""

import os
from functools import lru_cache


@lru_cache
def get_config() -> dict:
    """从环境变量读取配置。"""
    return {
        "app_env": os.getenv("APP_ENV", "dev"),
        "app_port": int(os.getenv("APP_PORT", "8000")),
        "log_level": os.getenv("LOG_LEVEL", "info"),
        "database_url": os.getenv("DATABASE_URL", ""),
        "ai_service_token": os.getenv("AI_SERVICE_TOKEN", ""),
        "dashscope_api_key": os.getenv("DASHSCOPE_API_KEY", ""),
        "bailian_base_url": os.getenv(
            "BAILIAN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
        "llm_model_router": os.getenv("LLM_MODEL_ROUTER", "qwen-flash"),
        "llm_model_qa": os.getenv("LLM_MODEL_QA", "qwen-plus"),
        "llm_model_troubleshoot": os.getenv("LLM_MODEL_TROUBLESHOOT", "qwen-plus"),
        "llm_model_checker": os.getenv("LLM_MODEL_CHECKER", "qwen-plus"),
        "llm_model_final": os.getenv("LLM_MODEL_FINAL", "qwen-plus"),
        "embedding_model": os.getenv("EMBEDDING_MODEL", "text-embedding-v4"),
        "embedding_dim": int(os.getenv("EMBEDDING_DIM", "1024")),
        # 检索参数（FR-14/15/16）
        "retrieval_top_k": int(os.getenv("RETRIEVAL_TOP_K", "5")),
        "retrieval_min_score": float(os.getenv("RETRIEVAL_MIN_SCORE", "0.35")),
        "hnsw_ef_search": int(os.getenv("HNSW_EF_SEARCH", "100")),
        # 检索主题一致性过滤（V1.1 主药，ADR-0002）
        #
        # 默认 **true**（2026-10-27 验收裁定）—— 理由比"安全"更实际：
        #   ① **部署一致性**：不开的话，陌生人 clone 下来跑到的行为与评测报告不符
        #      （报告说越界 −91.6%，他跑出来是未过滤的旧行为），等于"报告与代码不一致"；
        #   ② 本项目的**种子语料（5 篇）就是标定语料**，demo 场景下 on 才是正确基线。
        #
        # ⚠️ **风险已知且已记录**：`min_consistency=1.0` 过拟合本语料（ADR-0002 §6.2）。
        #    上传**新的文档集**前必须重扫阈值（`scripts/spike_v11/s5b_threshold_scan.py`），
        #    否则可能过严（误杀正确 chunk）或过松（放行污染）。
        #    置 `TOPIC_FILTER_ENABLED=false` 可整层关闭。
        "topic_filter_enabled": os.getenv("TOPIC_FILTER_ENABLED", "true").lower()
        in ("1", "true", "yes"),
        "topic_filter_min_consistency": float(
            os.getenv("TOPIC_FILTER_MIN_CONSISTENCY", "1.0")
        ),
        # 生成参数
        "llm_timeout_seconds": float(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
        "answer_max_tokens": int(os.getenv("ANSWER_MAX_TOKENS", "512")),  # 方案 A：原 1024
    }
