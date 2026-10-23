package handlers

import (
	"github.com/gin-gonic/gin"
	"opspilot/backend/internal/middleware"
)

// AskMessageStream 提问（SSE 流式，S7 实现）。
func AskMessageStream(c *gin.Context) {
	middleware.AbortWithError(c, 501, "NOT_IMPLEMENTED", "该端点已在 OpenAPI 注册，业务逻辑尚未实现")
}

// GetRun 执行轨迹（S6 实现）。
func GetRun(c *gin.Context) {
	middleware.AbortWithError(c, 501, "NOT_IMPLEMENTED", "该端点已在 OpenAPI 注册，业务逻辑尚未实现")
}
