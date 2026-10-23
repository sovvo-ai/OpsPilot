package handlers

import (
	"database/sql"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"opspilot/backend/internal/chat"
	"opspilot/backend/internal/middleware"
)

// ownerOf 从上下文取当前用户；取不到返回空串。
func ownerOf(c *gin.Context) string {
	v, _ := c.Get("user_id")
	s, _ := v.(string)
	return s
}

// handleChatErr 把 chat.HTTPError 映射为统一错误体。
func handleChatErr(c *gin.Context, err error) {
	if he, ok := err.(*chat.HTTPError); ok {
		middleware.AbortWithError(c, he.Status, he.Code, he.Msg)
		return
	}
	middleware.AbortWithError(c, http.StatusInternalServerError, "INTERNAL_ERROR", err.Error())
}

// CreateSession 创建会话。
func CreateSession(c *gin.Context) {
	ownerID := ownerOf(c)
	if ownerID == "" {
		middleware.AbortWithError(c, http.StatusUnauthorized, "UNAUTHORIZED", "无法识别用户")
		return
	}
	var body struct {
		Title string `json:"title"`
	}
	_ = c.ShouldBindJSON(&body) // 请求体可选

	db := c.MustGet("db").(*sql.DB)
	s, err := chat.CreateSession(db, ownerID, body.Title)
	if err != nil {
		handleChatErr(c, err)
		return
	}
	c.JSON(http.StatusCreated, s)
}

// ListSessions 会话列表。
func ListSessions(c *gin.Context) {
	ownerID := ownerOf(c)
	limit, _ := strconv.Atoi(c.DefaultQuery("limit", "20"))
	var before *time.Time
	if b := c.Query("before"); b != "" {
		if t, err := time.Parse(time.RFC3339, b); err == nil {
			before = &t
		}
	}
	db := c.MustGet("db").(*sql.DB)
	items, err := chat.ListSessions(db, ownerID, limit, before)
	if err != nil {
		handleChatErr(c, err)
		return
	}
	if items == nil {
		items = []chat.Session{}
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}

// GetSession 会话详情。
func GetSession(c *gin.Context) {
	ownerID := ownerOf(c)
	db := c.MustGet("db").(*sql.DB)
	s, err := chat.GetSession(db, ownerID, c.Param("session_id"))
	if err != nil {
		handleChatErr(c, err)
		return
	}
	c.JSON(http.StatusOK, s)
}

// UpdateSession 重命名会话。
func UpdateSession(c *gin.Context) {
	ownerID := ownerOf(c)
	var body struct {
		Title string `json:"title"`
	}
	if err := c.ShouldBindJSON(&body); err != nil || body.Title == "" {
		middleware.AbortWithError(c, http.StatusUnprocessableEntity, "INVALID_REQUEST", "缺少 title")
		return
	}
	db := c.MustGet("db").(*sql.DB)
	s, err := chat.UpdateSession(db, ownerID, c.Param("session_id"), body.Title)
	if err != nil {
		handleChatErr(c, err)
		return
	}
	c.JSON(http.StatusOK, s)
}

// DeleteSession 删除会话。
func DeleteSession(c *gin.Context) {
	ownerID := ownerOf(c)
	db := c.MustGet("db").(*sql.DB)
	if err := chat.DeleteSession(db, ownerID, c.Param("session_id")); err != nil {
		handleChatErr(c, err)
		return
	}
	c.Status(http.StatusNoContent)
}

// ListMessages 消息历史（含引用快照与悬挂标注）。
func ListMessages(c *gin.Context) {
	ownerID := ownerOf(c)
	limit, _ := strconv.Atoi(c.DefaultQuery("limit", "50"))
	db := c.MustGet("db").(*sql.DB)
	items, err := chat.ListMessages(db, ownerID, c.Param("session_id"), limit)
	if err != nil {
		handleChatErr(c, err)
		return
	}
	if items == nil {
		items = []map[string]any{}
	}
	c.JSON(http.StatusOK, gin.H{"items": items})
}
