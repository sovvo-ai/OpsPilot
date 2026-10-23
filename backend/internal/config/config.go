package config

import (
	"fmt"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

// Config 汇总后端运行配置。
type Config struct {
	AppEnv        string
	AppPort       string
	LogLevel      string
	DatabaseURL   string
	AIServiceURL  string
	AIServiceToken string
	JWTSecret     string
	EnableDevAuth bool
}

func FromEnv() (*Config, error) {
	cfg := &Config{
		AppEnv:         getEnv("APP_ENV", "dev"),
		AppPort:        getEnv("APP_PORT", "8081"),
		LogLevel:       getEnv("LOG_LEVEL", "info"),
		DatabaseURL:    buildDatabaseURL(),
		AIServiceURL:   getEnv("AI_SERVICE_URL", "http://ai-service:8000"),
		AIServiceToken: getEnv("AI_SERVICE_TOKEN", ""),
		JWTSecret:      getEnv("JWT_SECRET", ""),
		EnableDevAuth:  getEnvBool("ENABLE_DEV_AUTH", true),
	}

	if cfg.JWTSecret == "" {
		return nil, fmt.Errorf("JWT_SECRET 未设置")
	}
	if cfg.AIServiceToken == "" {
		return nil, fmt.Errorf("AI_SERVICE_TOKEN 未设置")
	}

	return cfg, nil
}

func buildDatabaseURL() string {
	if dsn := os.Getenv("DATABASE_URL"); dsn != "" {
		return dsn
	}

	user := getEnv("POSTGRES_USER", "ops_pilot")
	password := getEnv("POSTGRES_PASSWORD", "change-me-local-only")
	host := getEnv("POSTGRES_HOST", "localhost")
	port := getEnv("POSTGRES_PORT", "55432")
	if host == "localhost" && port == "5432" {
		// 本地 dev 默认走宿主端口
		port = getEnv("POSTGRES_HOST_PORT", "55432")
	}
	db := getEnv("POSTGRES_DB", "ops_pilot")
	sslmode := getEnv("POSTGRES_SSLMODE", "disable")

	return fmt.Sprintf(
		"postgres://%s:%s@%s:%s/%s?sslmode=%s",
		url.PathEscape(user),
		url.PathEscape(password),
		host,
		port,
		db,
		sslmode,
	)
}

func getEnv(key, defaultValue string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return defaultValue
}

func getEnvBool(key string, defaultValue bool) bool {
	v := strings.ToLower(os.Getenv(key))
	if v == "" {
		return defaultValue
	}
	b, err := strconv.ParseBool(v)
	if err != nil {
		return defaultValue
	}
	return b
}

// RequestTimeout 是调用 AI 服务的外向超时。
func (c *Config) RequestTimeout() time.Duration {
	return 10 * time.Second
}
