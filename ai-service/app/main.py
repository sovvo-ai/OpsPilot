"""OpsPilot AI Service — S3 骨架。

提供健康检查与内部接口占位，不含业务实现。
"""

import os

from fastapi import FastAPI

from app.middleware import InternalAuthMiddleware, RequestIDMiddleware
from app.routers import health, internal

APP_VERSION = os.getenv("APP_VERSION", "dev")

app = FastAPI(title="OpsPilot AI Service", version=APP_VERSION)

# 中间件：request_id 透传、内部令牌鉴权
app.add_middleware(RequestIDMiddleware)
app.add_middleware(InternalAuthMiddleware)

# 路由
app.include_router(health.router)
app.include_router(internal.router)
