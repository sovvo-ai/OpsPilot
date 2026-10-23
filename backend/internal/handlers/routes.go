package handlers

import (
	"github.com/gin-gonic/gin"
)

// RegisterAPIRoutes 注册全部业务路由（含健康端点）。
func RegisterAPIRoutes(r *gin.Engine, health *HealthHandler) {
	// 健康检查（免鉴权）
	r.GET("/health/live", health.Live)
	r.GET("/health/ready", health.Ready)
	r.GET("/health/deps", health.Deps)

	// 认证
	r.POST("/api/v1/auth/dev-token", DevToken)

	// 文档
	r.POST("/api/v1/documents", UploadDocument)
	r.GET("/api/v1/documents/:doc_id", GetDocument)
	r.DELETE("/api/v1/documents/:doc_id", DeleteDocument)
	r.POST("/api/v1/documents/:doc_id/reingest", ReingestDocument)

	// 会话
	r.POST("/api/v1/sessions", CreateSession)
	r.GET("/api/v1/sessions", ListSessions)
	r.GET("/api/v1/sessions/:session_id", GetSession)
	r.PATCH("/api/v1/sessions/:session_id", UpdateSession)
	r.DELETE("/api/v1/sessions/:session_id", DeleteSession)

	// 消息
	r.POST("/api/v1/sessions/:session_id/messages", AskMessage)
	r.GET("/api/v1/sessions/:session_id/messages", ListMessages)
	r.POST("/api/v1/sessions/:session_id/messages/stream", AskMessageStream)

	// 轨迹
	r.GET("/api/v1/runs/:run_id", GetRun)
}
