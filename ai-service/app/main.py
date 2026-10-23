"""OpsPilot AI Service。

提供健康检查、内部摄取（切片 + Embedding + 不可见切片写入）接口。
"""

import logging
import os

from fastapi import FastAPI

from app.middleware import InternalAuthMiddleware, RequestIDMiddleware
from app.routers import health, internal

APP_VERSION = os.getenv("APP_VERSION", "dev")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(title="OpsPilot AI Service", version=APP_VERSION)

# 中间件：request_id 透传、内部令牌鉴权
app.add_middleware(RequestIDMiddleware)
app.add_middleware(InternalAuthMiddleware)

# 路由
app.include_router(health.router)
app.include_router(internal.router)
