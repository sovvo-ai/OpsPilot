package middleware

import (
	"crypto/subtle"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v5"
)

const (
	DevUserID = "dev-user-001"
)

// JWTAuth 验证 Bearer JWT；/health/* 豁免。
func JWTAuth(secret string, enableDev bool) gin.HandlerFunc {
	return func(c *gin.Context) {
		if isHealthPath(c.Request.URL.Path) {
			c.Next()
			return
		}

		// 内部接口使用 InternalAuth 中间件，不走 JWT
		if strings.HasPrefix(c.Request.URL.Path, "/internal/") {
			c.Next()
			return
		}

		tokenString := extractBearer(c.GetHeader("Authorization"))
		if tokenString == "" && enableDev {
			// dev 模式：无 token 时注入开发用户
			c.Set("user_id", DevUserID)
			c.Next()
			return
		}
		if tokenString == "" {
			AbortWithError(c, http.StatusUnauthorized, "UNAUTHORIZED", "缺少鉴权令牌")
			return
		}

		token, err := jwt.Parse(tokenString, func(token *jwt.Token) (interface{}, error) {
			if _, ok := token.Method.(*jwt.SigningMethodHMAC); !ok {
				return nil, jwt.ErrSignatureInvalid
			}
			return []byte(secret), nil
		}, jwt.WithValidMethods([]string{"HS256"}))

		if err != nil || !token.Valid {
			AbortWithError(c, http.StatusUnauthorized, "UNAUTHORIZED", "令牌无效或已过期")
			return
		}

		claims, ok := token.Claims.(jwt.MapClaims)
		if !ok {
			AbortWithError(c, http.StatusUnauthorized, "UNAUTHORIZED", "令牌声明解析失败")
			return
		}

		sub, ok := claims["sub"].(string)
		if !ok || sub == "" {
			AbortWithError(c, http.StatusUnauthorized, "UNAUTHORIZED", "令牌缺少 sub")
			return
		}

		c.Set("user_id", sub)
		c.Next()
	}
}

// InternalAuth 验证 X-Internal-Token；/health/* 豁免。
func InternalAuth(expectedToken string) gin.HandlerFunc {
	return func(c *gin.Context) {
		if isHealthPath(c.Request.URL.Path) {
			c.Next()
			return
		}

		if !strings.HasPrefix(c.Request.URL.Path, "/internal/") {
			c.Next()
			return
		}

		provided := c.GetHeader("X-Internal-Token")
		if subtle.ConstantTimeCompare([]byte(provided), []byte(expectedToken)) != 1 {
			AbortWithError(c, http.StatusUnauthorized, "INTERNAL_AUTH_FAILED", "内部令牌无效")
			return
		}
		c.Next()
	}
}

// RequestID 注入/透传 request_id。
func RequestID() gin.HandlerFunc {
	return func(c *gin.Context) {
		rid := c.GetHeader("X-Request-Id")
		if rid == "" {
			rid = generateRequestID()
		}
		c.Set("request_id", rid)
		c.Writer.Header().Set("X-Request-Id", rid)
		c.Next()
	}
}

func extractBearer(header string) string {
	const prefix = "Bearer "
	if strings.HasPrefix(header, prefix) {
		return strings.TrimSpace(header[len(prefix):])
	}
	return ""
}

func isHealthPath(path string) bool {
	return strings.HasPrefix(path, "/health/")
}

func generateRequestID() string {
	return time.Now().UTC().Format("20060102150405") + "-" + randomString(8)
}

func randomString(n int) string {
	const letters = "abcdefghijklmnopqrstuvwxyz0123456789"
	b := make([]byte, n)
	for i := range b {
		b[i] = letters[time.Now().UnixNano()%int64(len(letters))]
		time.Sleep(1 * time.Nanosecond)
	}
	return string(b)
}
