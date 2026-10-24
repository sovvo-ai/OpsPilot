package chat

import (
	"database/sql"
	"encoding/json"
)

// Citation 是引用快照（对齐 openapi-backend Citation schema）。
//
// **`Index` 必须保留**（S9 实测发现的装配缺陷）：ai-service 按正文角标 `[n]`
// 标注每条引用的 `index`，前端据此把正文角标映射到片段（**禁止用数组下标** ——
// 答案若先写 `[2]` 再写 `[1]`，数组顺序与角标号不一致，用下标会点开错片段）。
//
// 该字段曾因本 struct 未声明而被 `encoding/json` 静默丢弃：ai-service 侧有、
// Go 侧无、前端拿不到，表现为"点任何角标都提示未找到引用"。
// 与 D2（`retrieval_top_score` 未落库）同属"字段在写路径存在、传输路径丢失"。
type Citation struct {
	Index      int     `json:"index"`
	ChunkID    string  `json:"chunk_id"`
	DocID      string  `json:"doc_id"`
	DocTitle   string  `json:"doc_title"`
	Snippet    string  `json:"snippet"`
	Score      float64 `json:"score,omitempty"`
	SourceType string  `json:"source_type,omitempty"`
	ToolCallID string  `json:"tool_call_id,omitempty"`
	Status     string  `json:"status"`
}

// DecorateCitations 读取引用快照，并按当前库状态标注悬挂状态（A9 / FR-21）。

// 规则：
//   - 切片仍存在且文档未删 → live
//   - 文档已删（deleted_at 非空）或切片已清理 → deleted（保留快照，前端显示占位）
//   - 快照本身缺失 → orphaned
//
// 该函数保证"原文删除后历史消息仍可 200 返回"，不报错、不空白。
func DecorateCitations(db *sql.DB, ownerID, citationsJSON string) ([]Citation, error) {
	if citationsJSON == "" {
		return []Citation{}, nil
	}
	var raw []Citation
	if err := json.Unmarshal([]byte(citationsJSON), &raw); err != nil {
		return nil, err
	}
	out := make([]Citation, 0, len(raw))
	for _, c := range raw {
		status := "orphaned"
		if c.ChunkID != "" {
			var deletedAt sql.NullTime
			err := db.QueryRow(
				`SELECT d.deleted_at
				   FROM doc_chunks c
				   JOIN documents d ON d.id = c.doc_id
				  WHERE c.chunk_id = $1 AND d.owner_id = $2`,
				c.ChunkID, ownerID,
			).Scan(&deletedAt)
			switch {
			case err == sql.ErrNoRows:
				status = "deleted" // 切片已随删除清理，快照降级
			case err != nil:
				return nil, err
			case deletedAt.Valid:
				status = "deleted"
			default:
				status = "live"
			}
		}
		c.Status = status
		out = append(out, c)
	}
	return out, nil
}
