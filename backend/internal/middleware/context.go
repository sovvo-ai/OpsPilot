package middleware

import (
	"database/sql"

	"github.com/gin-gonic/gin"
)

// WithDB 将数据库连接注入 gin 上下文；db 为 nil 时 handlers 会拿到 nil。
func WithDB(db *sql.DB) gin.HandlerFunc {
	return func(c *gin.Context) {
		if db != nil {
			c.Set("db", db)
		}
		c.Next()
	}
}
