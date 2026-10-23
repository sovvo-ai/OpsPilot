package handlers

import (
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v5"
	"opspilot/backend/internal/config"
	"opspilot/backend/internal/middleware"
)

// devAuthWhitelist 是开发环境允许签发的身份白名单。
var devAuthWhitelist = map[string]bool{
	"demo-user":    true,
	"test-user-2":  true,
}

// AuthHandler 处理认证相关接口。
type AuthHandler struct {
	cfg *config.Config
}

// NewAuthHandler 创建认证处理器。
func NewAuthHandler(cfg *config.Config) *AuthHandler {
	return &AuthHandler{cfg: cfg}
}

// DevToken 签发开发用 JWT（仅 dev 环境）。
// query: user（可选，默认 demo-user），白名单限定为 demo-user / test-user-2。
// 返回 HS256 签名、2h 过期的 Bearer token。
func (h *AuthHandler) DevToken(c *gin.Context) {
	if h.cfg.AppEnv != "dev" || !h.cfg.EnableDevAuth {
		middleware.AbortWithError(c, http.StatusNotFound, "NOT_FOUND", "该接口在当前环境不可用")
		return
	}

	user := c.DefaultQuery("user", "demo-user")
	if !devAuthWhitelist[user] {
		middleware.AbortWithError(c, http.StatusBadRequest, "INVALID_USER", "非法的 dev 用户身份")
		return
	}

	now := time.Now()
	claims := jwt.MapClaims{
		"sub": user,
		"iat": now.Unix(),
		"exp": now.Add(2 * time.Hour).Unix(),
	}
	token := jwt.NewWithClaims(jwt.SigningMethodHS256, claims)
	tokenString, err := token.SignedString([]byte(h.cfg.JWTSecret))
	if err != nil {
		middleware.AbortWithError(c, http.StatusInternalServerError, "INTERNAL_ERROR", "令牌签发失败")
		return
	}

	c.JSON(http.StatusOK, gin.H{"token": tokenString, "expires_in": 7200})
}
