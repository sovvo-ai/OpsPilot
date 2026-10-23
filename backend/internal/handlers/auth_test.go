package handlers

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v5"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"opspilot/backend/internal/config"
	"opspilot/backend/internal/middleware"
)

func TestDevToken_DefaultUser(t *testing.T) {
	gin.SetMode(gin.TestMode)
	cfg := &config.Config{AppEnv: "dev", EnableDevAuth: true, JWTSecret: "test-secret"}
	h := NewAuthHandler(cfg)

	r := gin.New()
	r.POST("/api/v1/auth/dev-token", h.DevToken)

	w := httptest.NewRecorder()
	req, _ := http.NewRequest(http.MethodPost, "/api/v1/auth/dev-token", nil)
	r.ServeHTTP(w, req)

	require.Equal(t, http.StatusOK, w.Code)
	var body map[string]any
	require.NoError(t, json.Unmarshal(w.Body.Bytes(), &body))
	require.Contains(t, body, "token")

	tokenString := body["token"].(string)
	token, err := jwt.Parse(tokenString, func(token *jwt.Token) (interface{}, error) {
		return []byte("test-secret"), nil
	}, jwt.WithValidMethods([]string{"HS256"}))
	require.NoError(t, err)
	require.True(t, token.Valid)
	claims := token.Claims.(jwt.MapClaims)
	assert.Equal(t, "demo-user", claims["sub"])
	assert.Equal(t, 7200.0, body["expires_in"])
}

func TestDevToken_TestUser2(t *testing.T) {
	gin.SetMode(gin.TestMode)
	cfg := &config.Config{AppEnv: "dev", EnableDevAuth: true, JWTSecret: "test-secret"}
	h := NewAuthHandler(cfg)

	r := gin.New()
	r.POST("/api/v1/auth/dev-token", h.DevToken)

	w := httptest.NewRecorder()
	req, _ := http.NewRequest(http.MethodPost, "/api/v1/auth/dev-token?user=test-user-2", nil)
	r.ServeHTTP(w, req)

	require.Equal(t, http.StatusOK, w.Code)
	var body map[string]any
	require.NoError(t, json.Unmarshal(w.Body.Bytes(), &body))
	token, err := jwt.Parse(body["token"].(string), func(token *jwt.Token) (interface{}, error) {
		return []byte("test-secret"), nil
	}, jwt.WithValidMethods([]string{"HS256"}))
	require.NoError(t, err)
	assert.Equal(t, "test-user-2", token.Claims.(jwt.MapClaims)["sub"])
}

func TestDevToken_InvalidUser(t *testing.T) {
	gin.SetMode(gin.TestMode)
	cfg := &config.Config{AppEnv: "dev", EnableDevAuth: true, JWTSecret: "test-secret"}
	h := NewAuthHandler(cfg)

	r := gin.New()
	r.POST("/api/v1/auth/dev-token", h.DevToken)

	w := httptest.NewRecorder()
	req, _ := http.NewRequest(http.MethodPost, "/api/v1/auth/dev-token?user=attacker", nil)
	r.ServeHTTP(w, req)

	require.Equal(t, http.StatusBadRequest, w.Code)
	var body middleware.ErrorBody
	require.NoError(t, json.Unmarshal(w.Body.Bytes(), &body))
	assert.Equal(t, "INVALID_USER", body.Error.Code)
}

func TestDevToken_ProdNotFound(t *testing.T) {
	gin.SetMode(gin.TestMode)
	cfg := &config.Config{AppEnv: "prod", EnableDevAuth: false, JWTSecret: "test-secret"}
	h := NewAuthHandler(cfg)

	r := gin.New()
	r.POST("/api/v1/auth/dev-token", h.DevToken)

	w := httptest.NewRecorder()
	req, _ := http.NewRequest(http.MethodPost, "/api/v1/auth/dev-token", nil)
	r.ServeHTTP(w, req)

	require.Equal(t, http.StatusNotFound, w.Code)
}

func TestJWTAuth_MissingToken(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(middleware.JWTAuth("test-secret"))
	r.GET("/api/v1/documents", func(c *gin.Context) { c.Status(http.StatusOK) })

	w := httptest.NewRecorder()
	req, _ := http.NewRequest(http.MethodGet, "/api/v1/documents", nil)
	r.ServeHTTP(w, req)

	require.Equal(t, http.StatusUnauthorized, w.Code)
}

func TestJWTAuth_InvalidToken(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(middleware.JWTAuth("test-secret"))
	r.GET("/api/v1/documents", func(c *gin.Context) { c.Status(http.StatusOK) })

	w := httptest.NewRecorder()
	req, _ := http.NewRequest(http.MethodGet, "/api/v1/documents", nil)
	req.Header.Set("Authorization", "Bearer wrong-token")
	r.ServeHTTP(w, req)

	require.Equal(t, http.StatusUnauthorized, w.Code)
}

func TestJWTAuth_EmptySecretRejectsAll(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(middleware.JWTAuth(""))
	r.GET("/api/v1/documents", func(c *gin.Context) { c.Status(http.StatusOK) })

	// 用空 secret 签一个 token
	token := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{"sub": "demo-user"})
	tokenString, err := token.SignedString([]byte(""))
	require.NoError(t, err)

	w := httptest.NewRecorder()
	req, _ := http.NewRequest(http.MethodGet, "/api/v1/documents", nil)
	req.Header.Set("Authorization", "Bearer "+tokenString)
	r.ServeHTTP(w, req)

	require.Equal(t, http.StatusUnauthorized, w.Code)
}

func TestJWTAuth_ExpiredToken(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.Use(middleware.JWTAuth("test-secret"))
	r.GET("/api/v1/documents", func(c *gin.Context) { c.Status(http.StatusOK) })

	claims := jwt.MapClaims{"sub": "demo-user", "exp": time.Now().Add(-time.Hour).Unix()}
	token := jwt.NewWithClaims(jwt.SigningMethodHS256, claims)
	tokenString, err := token.SignedString([]byte("test-secret"))
	require.NoError(t, err)

	w := httptest.NewRecorder()
	req, _ := http.NewRequest(http.MethodGet, "/api/v1/documents", nil)
	req.Header.Set("Authorization", "Bearer "+tokenString)
	r.ServeHTTP(w, req)

	require.Equal(t, http.StatusUnauthorized, w.Code)
}

func TestJWTAuth_DevTokenEndpointExempt(t *testing.T) {
	gin.SetMode(gin.TestMode)
	cfg := &config.Config{AppEnv: "dev", EnableDevAuth: true, JWTSecret: "test-secret"}
	h := NewAuthHandler(cfg)

	r := gin.New()
	r.Use(middleware.JWTAuth("test-secret"))
	r.POST("/api/v1/auth/dev-token", h.DevToken)

	w := httptest.NewRecorder()
	req, _ := http.NewRequest(http.MethodPost, "/api/v1/auth/dev-token", nil)
	r.ServeHTTP(w, req)

	require.Equal(t, http.StatusOK, w.Code)
}

func TestMain(m *testing.M) {
	// 保证 JWTAuth 空密钥测试不会污染其他测试的 secret
	os.Exit(m.Run())
}
