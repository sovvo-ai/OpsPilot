// Package web 用 Go `embed` 托管前端单页（PRD §8.1：不引入第三套服务）。
//
// 为什么用 embed 而不是 Nginx：
//   - 少一个组件（部署面变小、少一处配置漂移风险）；
//   - 前端产物随二进制发布，**版本天然对齐**（不会出现"后端新、静态资源旧"）；
//   - 无构建链（原生 HTML/CSS/JS），无需 Node 参与。
//
// 托管规则：
//   - `/`            → index.html
//   - `/style.css`、`/app.js` 等 → static 目录下同名文件
//   - 其余非 `/api`、`/health` 路径 → 回落 index.html（hash 路由由前端处理）
package web

import (
	"embed"
	"io/fs"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
)

//go:embed static
var staticFS embed.FS

// Register 把前端挂到 gin 引擎上。
//
// 注意：**必须在业务路由之后注册**，否则 `/api/*` 会被静态回落吞掉。
func Register(r *gin.Engine) error {
	sub, err := fs.Sub(staticFS, "static")
	if err != nil {
		return err
	}
	files := http.FS(sub)

	serve := func(c *gin.Context, name string) {
		f, err := files.Open(name)
		if err != nil {
			// 回落 index.html（前端为 hash 路由单页）
			name = "index.html"
			f, err = files.Open(name)
			if err != nil {
				c.String(http.StatusNotFound, "not found")
				return
			}
		}
		defer f.Close()
		stat, err := f.Stat()
		if err != nil {
			c.String(http.StatusInternalServerError, "stat failed")
			return
		}
		// 首页不缓存（保证改版即生效）；其余静态资源短缓存
		if name == "index.html" {
			c.Header("Cache-Control", "no-cache")
		} else {
			c.Header("Cache-Control", "public, max-age=300")
		}
		http.ServeContent(c.Writer, c.Request, name, stat.ModTime(), f)
	}

	r.GET("/", func(c *gin.Context) { serve(c, "index.html") })
	r.GET("/index.html", func(c *gin.Context) { serve(c, "index.html") })
	r.GET("/style.css", func(c *gin.Context) { serve(c, "style.css") })
	r.GET("/app.js", func(c *gin.Context) { serve(c, "app.js") })

	// 兜底：非 API/健康路径回落首页（方便将来加图标等资源）
	r.NoRoute(func(c *gin.Context) {
		p := c.Request.URL.Path
		if strings.HasPrefix(p, "/api/") || strings.HasPrefix(p, "/health") ||
			strings.HasPrefix(p, "/internal/") {
			c.JSON(http.StatusNotFound, gin.H{
				"error": gin.H{"code": "NOT_FOUND", "message": "接口不存在"},
			})
			return
		}
		if c.Request.Method != http.MethodGet {
			c.JSON(http.StatusMethodNotAllowed, gin.H{
				"error": gin.H{"code": "METHOD_NOT_ALLOWED", "message": "方法不支持"},
			})
			return
		}
		serve(c, strings.TrimPrefix(p, "/"))
	})
	return nil
}
