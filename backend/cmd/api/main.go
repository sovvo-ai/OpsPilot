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
	"opspilot/backend/internal/buildinfo"
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

	// gin 运行模式：**环境变量 GIN_MODE 优先**（容器里由 Dockerfile 设为 release），
	// 其次按 APP_ENV 推导。早期实现无条件 `SetMode` 会覆盖 GIN_MODE=release，
	// 导致 release 镜像仍在 debug 模式（日志噪声 + 性能损耗）。
	if os.Getenv("GIN_MODE") == "" {
		if cfg.AppEnv == "prod" {
			gin.SetMode(gin.ReleaseMode)
		} else {
			gin.SetMode(gin.DebugMode)
		}
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

	// 构建信息（清 S3 挂账）：ldflags 注入，未注入时回落 dev/unknown
	version := buildinfo.Version
	if v := os.Getenv("APP_VERSION"); v != "" {
		version = v // 环境变量优先（容器可覆盖）
	}
	health := handlers.NewHealthHandler(database, version, buildinfo.BuildTime, cfg.AIServiceURL)
	log.Printf("启动: version=%s commit=%s build_time=%s mode=%s",
		version, buildinfo.Commit, buildinfo.BuildTime, gin.Mode())
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
