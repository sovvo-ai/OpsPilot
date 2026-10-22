package main

import (
	"os"
	"time"

	"github.com/gin-gonic/gin"
)

// S1 SPIKE 骨架：只提供 health 级端点，不含任何业务逻辑。
func main() {
	port := os.Getenv("APP_PORT")
	if port == "" {
		port = "8081"
	}
	r := gin.New()
	r.Use(gin.Recovery())

	r.GET("/health/live", func(c *gin.Context) {
		c.JSON(200, gin.H{
			"status":  "ok",
			"service": "ops-pilot-backend",
			"version": "spike",
			"time":    time.Now().UTC().Format(time.RFC3339),
		})
	})

	_ = r.Run(":" + port)
}
