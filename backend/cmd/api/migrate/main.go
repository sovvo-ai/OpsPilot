package main

import (
	"fmt"
	"log"
	"os"

	"opspilot/backend/internal/config"
	"opspilot/backend/internal/db"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Println("用法: go run ./cmd/api/migrate [up|down]")
		os.Exit(1)
	}

	cfg, err := config.FromEnv()
	if err != nil {
		log.Fatalf("配置错误: %v", err)
	}

	database, err := db.Open(cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("数据库连接失败: %v", err)
	}
	defer database.Close()

	// 迁移文件相对于 backend/ 目录
	database.SetMigrationDir("migrations")

	switch os.Args[1] {
	case "up":
		if err := database.MigrateUp(); err != nil {
			log.Fatalf("迁移 up 失败: %v", err)
		}
		log.Println("迁移 up 成功")
	case "down":
		if err := database.MigrateDown(); err != nil {
			log.Fatalf("迁移 down 失败: %v", err)
		}
		log.Println("迁移 down 成功")
	default:
		fmt.Println("用法: go run ./cmd/api/migrate [up|down]")
		os.Exit(1)
	}
}
