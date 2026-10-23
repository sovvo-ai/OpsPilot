package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"
	"opspilot/backend/internal/chat"
	"opspilot/backend/internal/config"
	"opspilot/backend/internal/db"
	"opspilot/backend/internal/handlers"
	"opspilot/backend/internal/ingest"
	"opspilot/backend/internal/middleware"
)

func main() {
	cfg, err := config.FromEnv()
	if err != nil {
		log.Fatalf("配置错误: %v", err)
	}

	if cfg.AppEnv == "prod" {
		gin.SetMode(gin.ReleaseMode)
	} else {
		gin.SetMode(gin.DebugMode)
	}

	// 数据库连接
	var database *db.DB
	if cfg.DatabaseURL != "" {
		d, err := db.Open(cfg.DatabaseURL)
		if err != nil {
			log.Printf("数据库连接失败: %v", err)
		} else {
			database = d
			if err := database.MigrateUp(); err != nil {
				log.Printf("迁移失败: %v", err)
			}
		}
	}

	r := gin.New()
	r.Use(gin.Recovery())
	r.Use(middleware.RequestID())
	r.Use(middleware.ErrorHandler())
	if database != nil {
		r.Use(middleware.WithDB(database.DB))
	}
	// S5：编排客户端（Jan 侧调 ai-service /internal/v1/chat）
	orch := chat.NewOrchestrator(cfg.AIServiceURL, cfg.AIServiceToken)
	r.Use(middleware.WithOrchestrator(orch))
	r.Use(middleware.JWTAuth(cfg.JWTSecret))
	r.Use(middleware.InternalAuth(cfg.AIServiceToken))

	version := handlers.ServiceVersion()
	health := handlers.NewHealthHandler(database, version, "", cfg.AIServiceURL)
	auth := handlers.NewAuthHandler(cfg)
	handlers.RegisterAPIRoutes(r, health, auth)

	var dispatcher *ingest.Dispatcher
	if database != nil {
		dispatcher = ingest.NewDispatcher(database.DB, cfg.AIServiceURL, cfg.AIServiceToken)
		dispatcher.Start()
	}

	// 404 也走统一结构
	r.NoRoute(func(c *gin.Context) {
		middleware.AbortWithError(c, http.StatusNotFound, "NOT_FOUND", "路径不存在")
	})

	srv := &http.Server{
		Addr:    ":" + cfg.AppPort,
		Handler: r,
	}

	go func() {
		log.Printf("backend 启动于 :%s", cfg.AppPort)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("服务启动失败: %v", err)
		}
	}()

	// 优雅关闭
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit

	log.Println("正在关闭服务...")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("关闭失败: %v", err)
	}
	if dispatcher != nil {
		dispatcher.Stop()
	}
	if database != nil {
		_ = database.Close()
	}
}

// AbortWithError 暴露给 main 的 no-route 处理。
func AbortWithError(c *gin.Context, status int, code, message string) {
	middleware.AbortWithError(c, status, code, message)
}
