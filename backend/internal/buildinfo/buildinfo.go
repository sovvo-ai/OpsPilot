// Package buildinfo 承载构建期注入的元信息（清 S3 挂账）。
//
// 通过 `go build -ldflags "-X ..."` 注入；见 backend/Dockerfile 与 Makefile。
// 未注入时回落为 "dev"/"unknown"，**不 panic**（本地 go run 也能跑）。
package buildinfo

// 以下变量仅由 -ldflags 注入，源码里不得赋值。
var (
	// Version 语义化版本或 git tag；未注入为 "dev"。
	Version = "dev"
	// Commit 短 SHA；未注入为 "unknown"。
	Commit = "unknown"
	// BuildTime 构建时刻（RFC3339）；未注入为 "unknown"。
	BuildTime = "unknown"
)

// Info 返回构建信息的只读视图（供 /health/* 与日志使用）。
func Info() map[string]string {
	return map[string]string{
		"version":    Version,
		"commit":     Commit,
		"build_time": BuildTime,
	}
}

// IsRelease 判断是否为"真实构建"（三项均已注入）。
func IsRelease() bool {
	return Version != "dev" && BuildTime != "unknown"
}