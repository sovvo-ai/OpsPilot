package handlers

import (
	"database/sql"

	"github.com/gin-gonic/gin"
	"opspilot/backend/internal/chat"
	"opspilot/backend/internal/middleware"
)

// GetRun 执行轨迹（A8：节点顺序 + 耗时 + 模型调用 + 工具调用）。
func GetRun(c *gin.Context) {
	ownerID := ownerOf(c)
	if ownerID == "" {
		middleware.AbortWithError(c, 401, "UNAUTHORIZED", "无法识别用户")
		return
	}
	db := c.MustGet("db").(*sql.DB)
	run, err := chat.GetRun(db, ownerID, c.Param("run_id"))
	if err != nil {
		handleChatErr(c, err)
		return
	}
	c.JSON(200, run)
}
