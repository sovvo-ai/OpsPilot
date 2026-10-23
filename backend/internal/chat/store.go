package chat

import (
	"database/sql"
	"errors"
	"fmt"
	"net/http"
	"time"
)

// HTTPError 直接映射为 HTTP 4xx。
type HTTPError struct {
	Status int
	Code   string
	Msg    string
}

func (e *HTTPError) Error() string { return e.Code }

// Session 是 sessions 行映射。
type Session struct {
	ID           string    `json:"id"`
	Title        string    `json:"title"`
	MessageCount int       `json:"message_count"`
	CreatedAt    time.Time `json:"created_at"`
	UpdatedAt    time.Time `json:"updated_at"`
}

// CreateSession 创建会话。
func CreateSession(db *sql.DB, ownerID, title string) (*Session, error) {
	if title == "" {
		title = "新会话"
	}
	if len([]rune(title)) > 200 {
		return nil, &HTTPError{Status: http.StatusUnprocessableEntity, Code: "INVALID_TITLE", Msg: "标题超长"}
	}
	var s Session
	err := db.QueryRow(
		`INSERT INTO sessions (owner_id, title) VALUES ($1, $2)
		 RETURNING id, title, created_at, updated_at`,
		ownerID, title,
	).Scan(&s.ID, &s.Title, &s.CreatedAt, &s.UpdatedAt)
	if err != nil {
		return nil, fmt.Errorf("insert session: %w", err)
	}
	return &s, nil
}

// ListSessions 按最近活跃列出本人会话。
func ListSessions(db *sql.DB, ownerID string, limit int, before *time.Time) ([]Session, error) {
	if limit <= 0 || limit > 100 {
		limit = 20
	}
	q := `SELECT s.id, s.title, s.created_at, s.updated_at,
	             (SELECT count(*) FROM messages m WHERE m.session_id = s.id) AS message_count
	        FROM sessions s
	       WHERE s.owner_id = $1`
	args := []any{ownerID}
	if before != nil {
		q += " AND s.updated_at < $2"
		args = append(args, *before)
	}
	q += " ORDER BY s.updated_at DESC LIMIT $" + fmt.Sprint(len(args)+1)
	args = append(args, limit)

	rows, err := db.Query(q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []Session
	for rows.Next() {
		var s Session
		if err := rows.Scan(&s.ID, &s.Title, &s.CreatedAt, &s.UpdatedAt, &s.MessageCount); err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, rows.Err()
}

// GetSession 读取会话，校验 owner（跨用户返回不存在，不泄露存在性）。
func GetSession(db *sql.DB, ownerID, sessionID string) (*Session, error) {
	var s Session
	err := db.QueryRow(
		`SELECT s.id, s.title, s.created_at, s.updated_at,
		        (SELECT count(*) FROM messages m WHERE m.session_id = s.id)
		   FROM sessions s
		  WHERE s.id = $1 AND s.owner_id = $2`,
		sessionID, ownerID,
	).Scan(&s.ID, &s.Title, &s.CreatedAt, &s.UpdatedAt, &s.MessageCount)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, &HTTPError{Status: http.StatusNotFound, Code: "NOT_FOUND", Msg: "会话不存在"}
	}
	if err != nil {
		return nil, err
	}
	return &s, nil
}

// UpdateSession 重命名会话。
func UpdateSession(db *sql.DB, ownerID, sessionID, title string) (*Session, error) {
	res, err := db.Exec(
		`UPDATE sessions SET title = $1, updated_at = now()
		  WHERE id = $2 AND owner_id = $3`,
		title, sessionID, ownerID,
	)
	if err != nil {
		return nil, err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return nil, &HTTPError{Status: http.StatusNotFound, Code: "NOT_FOUND", Msg: "会话不存在"}
	}
	return GetSession(db, ownerID, sessionID)
}

// DeleteSession 删除会话（消息级联删除）。
func DeleteSession(db *sql.DB, ownerID, sessionID string) error {
	res, err := db.Exec(`DELETE FROM sessions WHERE id = $1 AND owner_id = $2`, sessionID, ownerID)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return &HTTPError{Status: http.StatusNotFound, Code: "NOT_FOUND", Msg: "会话不存在"}
	}
	return nil
}

// Message 是 messages 行映射（含引用快照）。
type Message struct {
	ID        string
	SessionID string
	Role      string
	Content   string
	Citations string // JSON 文本
	Degraded  bool
	RunID     *string
	CreatedAt time.Time
}

// HistoryMessage 是喂给编排的上下文消息。
type HistoryMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// RecentHistory 取会话最近 n 条消息，按时间升序返回（供组装最近 4 轮上下文）。
func RecentHistory(db *sql.DB, sessionID string, n int) ([]HistoryMessage, error) {
	rows, err := db.Query(
		`SELECT role, content FROM (
		     SELECT role, content, created_at FROM messages
		      WHERE session_id = $1
		      ORDER BY created_at DESC
		      LIMIT $2
		 ) t ORDER BY created_at ASC`,
		sessionID, n,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []HistoryMessage
	for rows.Next() {
		var m HistoryMessage
		if err := rows.Scan(&m.Role, &m.Content); err != nil {
			return nil, err
		}
		out = append(out, m)
	}
	return out, rows.Err()
}

// CountTurns 统计会话中用户消息数，作为 turn_index（首轮为 0）。
func CountTurns(db *sql.DB, sessionID string) (int, error) {
	var n int
	err := db.QueryRow(
		`SELECT count(*) FROM messages WHERE session_id = $1 AND role = 'user'`,
		sessionID,
	).Scan(&n)
	return n, err
}

// InsertMessage 写入一条消息，返回主键。
func InsertMessage(db *sql.DB, sessionID, role, content, citationsJSON string, degraded bool) (string, error) {
	var id string
	err := db.QueryRow(
		`INSERT INTO messages (session_id, role, content, citations, degraded)
		 VALUES ($1, $2, $3, $4::jsonb, $5) RETURNING id`,
		sessionID, role, content, citationsJSON, degraded,
	).Scan(&id)
	return id, err
}

// InsertAssistantWithRun 写入助手消息并关联 run_id，同时落 agent_runs。
func InsertAssistantWithRun(
	db *sql.DB, sessionID, ownerID, content, citationsJSON string, degraded bool, runID string,
) (messageID string, err error) {
	tx, err := db.Begin()
	if err != nil {
		return "", err
	}
	defer tx.Rollback()

	err = tx.QueryRow(
		`INSERT INTO messages (session_id, role, content, citations, degraded, run_id)
		 VALUES ($1, 'assistant', $2, $3::jsonb, $4, $5) RETURNING id`,
		sessionID, content, citationsJSON, degraded, runID,
	).Scan(&messageID)
	if err != nil {
		return "", err
	}

	if _, err = tx.Exec(
		`INSERT INTO agent_runs (id, session_id, message_id, owner_id, route)
		 VALUES ($1, $2, $3, $4, 'kb_qa')`,
		runID, sessionID, messageID, ownerID,
	); err != nil {
		return "", err
	}

	if err = tx.Commit(); err != nil {
		return "", err
	}
	return messageID, nil
}

// TouchSession 刷新会话 updated_at（用于排序活跃度）。
func TouchSession(db *sql.DB, sessionID string) error {
	_, err := db.Exec(`UPDATE sessions SET updated_at = now() WHERE id = $1`, sessionID)
	return err
}

// ListMessages 读取会话消息（时间升序），含引用快照与 A9 删除悬挂标记。
func ListMessages(db *sql.DB, ownerID, sessionID string, limit int) ([]map[string]any, error) {
	// 先校验会话归属
	if _, err := GetSession(db, ownerID, sessionID); err != nil {
		return nil, err
	}
	if limit <= 0 || limit > 100 {
		limit = 50
	}

	rows, err := db.Query(
		`SELECT id, role, content, citations, degraded, run_id, created_at
		   FROM messages
		  WHERE session_id = $1
		  ORDER BY created_at ASC
		  LIMIT $2`,
		sessionID, limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var out []map[string]any
	for rows.Next() {
		var (
			id, role, content, citationsJSON string
			degraded                         bool
			runID                            *string
			createdAt                        time.Time
		)
		if err := rows.Scan(&id, &role, &content, &citationsJSON, &degraded, &runID, &createdAt); err != nil {
			return nil, err
		}
		cits, err := DecorateCitations(db, ownerID, citationsJSON)
		if err != nil {
			return nil, err
		}
		msg := map[string]any{
			"id":         id,
			"session_id": sessionID,
			"role":       role,
			"content":    content,
			"citations":  cits,
			"degraded":   degraded,
			"created_at": createdAt,
		}
		if runID != nil {
			msg["run_id"] = *runID
		}
		out = append(out, msg)
	}
	return out, rows.Err()
}
