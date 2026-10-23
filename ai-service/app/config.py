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
        # 生成参数
        "llm_timeout_seconds": float(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
        "answer_max_tokens": int(os.getenv("ANSWER_MAX_TOKENS", "1024")),
    }
