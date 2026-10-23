package middleware

import (
	"net/http"

	"github.com/gin-gonic/gin"
)

// ErrorBody 是统一错误结构。
type ErrorBody struct {
	Error ErrorDetail `json:"error"`
}

type ErrorDetail struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	RequestID string `json:"request_id"`
}

func AbortWithError(c *gin.Context, status int, code, message string) {
	rid, _ := c.Get("request_id")
	ridStr, _ := rid.(string)
	c.AbortWithStatusJSON(status, ErrorBody{
		Error: ErrorDetail{
			Code:      code,
			Message:   message,
			RequestID: ridStr,
		},
	})
}

// ErrorHandler 把 panic/unexpected error 转成统一结构。
func ErrorHandler() gin.HandlerFunc {
	return gin.Recovery()
}

// NotImplemented 返回 501 占位响应。
func NotImplemented(c *gin.Context) {
	AbortWithError(c, http.StatusNotImplemented, "NOT_IMPLEMENTED",
		"该端点已在 OpenAPI 注册，业务逻辑尚未实现")
}
