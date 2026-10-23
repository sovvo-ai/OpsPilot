package handlers

import (
	"github.com/gin-gonic/gin"
	"opspilot/backend/internal/middleware"
)

// DevToken 返回开发用 JWT（仅 dev 环境）。
func DevToken(c *gin.Context) {
	middleware.AbortWithError(c, 501, "NOT_IMPLEMENTED", "该端点已在 OpenAPI 注册，业务逻辑尚未实现")
}

// UploadDocument 上传文档。
func UploadDocument(c *gin.Context) {
	middleware.AbortWithError(c, 501, "NOT_IMPLEMENTED", "该端点已在 OpenAPI 注册，业务逻辑尚未实现")
}

// GetDocument 查询文档。
func GetDocument(c *gin.Context) {
	middleware.AbortWithError(c, 501, "NOT_IMPLEMENTED", "该端点已在 OpenAPI 注册，业务逻辑尚未实现")
}

// DeleteDocument 删除文档。
func DeleteDocument(c *gin.Context) {
	middleware.AbortWithError(c, 501, "NOT_IMPLEMENTED", "该端点已在 OpenAPI 注册，业务逻辑尚未实现")
}

// ReingestDocument 手动重试摄取。
func ReingestDocument(c *gin.Context) {
	middleware.AbortWithError(c, 501, "NOT_IMPLEMENTED", "该端点已在 OpenAPI 注册，业务逻辑尚未实现")
}

// CreateSession 创建会话。
func CreateSession(c *gin.Context) {
	middleware.AbortWithError(c, 501, "NOT_IMPLEMENTED", "该端点已在 OpenAPI 注册，业务逻辑尚未实现")
}

// ListSessions 会话列表。
func ListSessions(c *gin.Context) {
	middleware.AbortWithError(c, 501, "NOT_IMPLEMENTED", "该端点已在 OpenAPI 注册，业务逻辑尚未实现")
}

// GetSession 会话详情。
func GetSession(c *gin.Context) {
	middleware.AbortWithError(c, 501, "NOT_IMPLEMENTED", "该端点已在 OpenAPI 注册，业务逻辑尚未实现")
}

// UpdateSession 重命名会话。
func UpdateSession(c *gin.Context) {
	middleware.AbortWithError(c, 501, "NOT_IMPLEMENTED", "该端点已在 OpenAPI 注册，业务逻辑尚未实现")
}

// DeleteSession 删除会话。
func DeleteSession(c *gin.Context) {
	middleware.AbortWithError(c, 501, "NOT_IMPLEMENTED", "该端点已在 OpenAPI 注册，业务逻辑尚未实现")
}

// AskMessage 提问（非流式）。
func AskMessage(c *gin.Context) {
	middleware.AbortWithError(c, 501, "NOT_IMPLEMENTED", "该端点已在 OpenAPI 注册，业务逻辑尚未实现")
}

// ListMessages 消息历史。
func ListMessages(c *gin.Context) {
	middleware.AbortWithError(c, 501, "NOT_IMPLEMENTED", "该端点已在 OpenAPI 注册，业务逻辑尚未实现")
}

// AskMessageStream 提问（SSE 流式）。
func AskMessageStream(c *gin.Context) {
	middleware.AbortWithError(c, 501, "NOT_IMPLEMENTED", "该端点已在 OpenAPI 注册，业务逻辑尚未实现")
}

// GetRun 执行轨迹。
func GetRun(c *gin.Context) {
	middleware.AbortWithError(c, 501, "NOT_IMPLEMENTED", "该端点已在 OpenAPI 注册，业务逻辑尚未实现")
}
