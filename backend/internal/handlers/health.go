package handlers

import (
	"context"
	"database/sql"
	"fmt"
	"net/http"
	"os"
	"time"

	"github.com/gin-gonic/gin"
	"opspilot/backend/internal/db"
)

// HealthHandler 处理三级健康检查。
type HealthHandler struct {
	DB           *db.DB
	Version      string
	BuildTime    string
	aiServiceURL string
}

func NewHealthHandler(database *db.DB, version, buildTime, aiServiceURL string) *HealthHandler {
	return &HealthHandler{
		DB:           database,
		Version:      version,
		BuildTime:    buildTime,
		aiServiceURL: aiServiceURL,
	}
}

// Live 永远 200，仅表示进程存活。
func (h *HealthHandler) Live(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{
		"status":    "ok",
		"service":   "ops-pilot-backend",
		"version":   h.Version,
		"time":      time.Now().UTC().Format(time.RFC3339),
		"build_time": h.BuildTime,
	})
}

// Ready 检查数据库、迁移版本与最近一次 provider 探测结果。
func (h *HealthHandler) Ready(c *gin.Context) {
	ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
	defer cancel()
	result := h.checkAll(ctx)
	if !result.Healthy {
		c.JSON(http.StatusServiceUnavailable, result)
		return
	}
	c.JSON(http.StatusOK, result)
}

// Deps 返回依赖明细。
func (h *HealthHandler) Deps(c *gin.Context) {
	ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
	defer cancel()
	result := h.checkAll(ctx)
	c.JSON(http.StatusOK, result)
}

type HealthResult struct {
	Status    string                 `json:"status"`
	Service   string                 `json:"service"`
	Version   string                 `json:"version"`
	Time      string                 `json:"time"`
	Healthy   bool                   `json:"healthy"`
	Checks    map[string]HealthCheck `json:"checks"`
}

type HealthCheck struct {
	Status  string `json:"status"`
	Detail  string `json:"detail,omitempty"`
	Latency int64  `json:"latency_ms"`
}

func (h *HealthHandler) checkAll(ctx context.Context) *HealthResult {
	checks := make(map[string]HealthCheck)
	now := time.Now().UTC()
	healthy := true

	// 1. 数据库连通性
	dbStart := time.Now()
	dbStatus := "ok"
	dbDetail := ""
	if h.DB != nil {
		if err := h.DB.PingContext(ctx); err != nil {
			dbStatus = "error"
			dbDetail = err.Error()
			healthy = false
		}
	} else {
		dbStatus = "error"
		dbDetail = "未初始化"
		healthy = false
	}
	checks["database"] = HealthCheck{Status: dbStatus, Detail: dbDetail, Latency: time.Since(dbStart).Milliseconds()}

	// 2. 迁移版本
	migrationStatus := "ok"
	migrationDetail := ""
	if h.DB != nil {
		version, err := h.latestMigrationVersion()
		if err != nil {
			migrationStatus = "error"
			migrationDetail = err.Error()
			healthy = false
		} else if version == "" {
			migrationStatus = "error"
			migrationDetail = "未应用任何迁移"
			healthy = false
		} else {
			migrationDetail = version
		}
	} else {
		migrationStatus = "error"
		migrationDetail = "数据库未初始化"
		healthy = false
	}
	checks["migrations"] = HealthCheck{Status: migrationStatus, Detail: migrationDetail}

	// 3. AI 服务探测（最近一次，非阻塞）
	aiStart := time.Now()
	aiStatus := "ok"
	aiDetail := ""
	if h.aiServiceURL != "" {
		client := &http.Client{
			Timeout: 3 * time.Second,
			Transport: &http.Transport{Proxy: nil}, // 禁用 HTTP_PROXY，避免宿主编织的代理干扰
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, h.aiServiceURL+"/health/live", nil)
		if err != nil {
			aiStatus = "error"
			aiDetail = err.Error()
			healthy = false
		} else {
			resp, err := client.Do(req)
			if err != nil {
				aiStatus = "error"
				aiDetail = err.Error()
				healthy = false
			} else {
				resp.Body.Close()
				if resp.StatusCode != http.StatusOK {
					aiStatus = "error"
					aiDetail = fmt.Sprintf("status %d", resp.StatusCode)
					healthy = false
				}
			}
		}
	} else {
		aiStatus = "unknown"
		aiDetail = "AI_SERVICE_URL 未配置"
	}
	checks["ai_service"] = HealthCheck{Status: aiStatus, Detail: aiDetail, Latency: time.Since(aiStart).Milliseconds()}

	status := "ok"
	if !healthy {
		status = "degraded"
	}

	return &HealthResult{
		Status:  status,
		Service: "ops-pilot-backend",
		Version: h.Version,
		Time:    now.Format(time.RFC3339),
		Healthy: healthy,
		Checks:  checks,
	}
}

func (h *HealthHandler) latestMigrationVersion() (string, error) {
	var version string
	err := h.DB.QueryRow(`
		SELECT version FROM schema_migrations
		WHERE EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'schema_migrations')
		ORDER BY version DESC LIMIT 1
	`).Scan(&version)
	if err == sql.ErrNoRows {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	return version, nil
}

// ServiceVersion 从环境变量取版本，缺省为 dev。
func ServiceVersion() string {
	if v := os.Getenv("APP_VERSION"); v != "" {
		return v
	}
	return "dev"
}
