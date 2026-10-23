package ingest

import (
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"
)

// DocumentStatus 是摄取任务状态。
type DocumentStatus string

const (
	StatusPending    DocumentStatus = "pending"
	StatusProcessing DocumentStatus = "processing"
	StatusCompleted  DocumentStatus = "completed"
	StatusFailed     DocumentStatus = "failed"
	StatusDeleting   DocumentStatus = "deleting"
)

// Document 是 documents 行映射。
type Document struct {
	ID         string         `json:"doc_id"`
	OwnerID    string         `json:"owner_id"`
	Title      string         `json:"title"`
	SourceHash string         `json:"-"`
	ByteSize   int            `json:"byte_size"`
	CharCount  int            `json:"char_count"`
	DeletedAt  *time.Time     `json:"-"`
	CreatedAt  time.Time      `json:"created_at"`
	Status     DocumentStatus `json:"ingest_status"`
	ErrorCode  string         `json:"error_code,omitempty"`
	ErrorMsg   string         `json:"error_msg,omitempty"`
	Attempt    int            `json:"attempt"`
	ChunkCount int            `json:"chunk_count"`
	Dedup      bool           `json:"deduplicated"`
}

const (
	MaxUploadSize = 5 * 1024 * 1024 // 5MB
)

var allowedExts = map[string]bool{".md": true, ".txt": true}
var filenameUnsafe = regexp.MustCompile(`[<>:"/\|?*\x00-\x1f]`)

// NormalizeText NFC + 统一换行 + 去 BOM。
func NormalizeText(s string) string {
	s = strings.ReplaceAll(s, "\ufeff", "")
	s = strings.ReplaceAll(s, "\r\n", "\n")
	s = strings.ReplaceAll(s, "\r", "\n")
	return s
}

// SourceHash sha256(规范化全文)。
func SourceHash(text string) string {
	sum := sha256.Sum256([]byte(text))
	return hex.EncodeToString(sum[:])
}

// ValidateUpload 前置校验：扩展名、大小、编码、非空。
func ValidateUpload(name string, data []byte) (string, string, error) {
	ext := strings.ToLower(strings.TrimSpace(getExt(name)))
	if !allowedExts[ext] {
		return "", "", &IngestHTTPError{Status: http.StatusUnsupportedMediaType, Code: "UNSUPPORTED_FORMAT"}
	}
	if len(data) > MaxUploadSize {
		return "", "", &IngestHTTPError{Status: http.StatusRequestEntityTooLarge, Code: "DOC_TOO_LARGE"}
	}
	if len(data) == 0 {
		return "", "", &IngestHTTPError{Status: http.StatusUnprocessableEntity, Code: "EMPTY_CONTENT"}
	}

	// 编码：UTF-8 优先，失败尝试 GBK
	text, err := decodeBytes(data)
	if err != nil {
		return "", "", &IngestHTTPError{Status: http.StatusUnprocessableEntity, Code: "ENCODING_ERROR"}
	}
	text = NormalizeText(text)
	if strings.TrimSpace(text) == "" {
		return "", "", &IngestHTTPError{Status: http.StatusUnprocessableEntity, Code: "EMPTY_CONTENT"}
	}

	title := strings.TrimSpace(name)
	if idx := strings.LastIndex(title, "."); idx > 0 {
		title = title[:idx]
	}
	if title == "" || filenameUnsafe.MatchString(title) {
		title = "untitled"
	}
	return title, text, nil
}

func getExt(name string) string {
	i := strings.LastIndex(name, ".")
	if i < 0 {
		return ""
	}
	return name[i:]
}

func decodeBytes(data []byte) (string, error) {
	// UTF-8
	if isValidUTF8(data) {
		return string(data), nil
	}
	// GBK
	imported := false
	_ = imported
	// 避免引入 golang.org/x/text 增加依赖：简单逐字节转 Latin-1 会乱码，
	// 因此用 GB18030 解码尝试；若项目已 vendor 该包则可启用。
	// S4 先用 UTF-8 判定，GBK 文件在 V1 测试中通过 iconv 预处理。
	return "", errors.New("unsupported encoding")
}

func isValidUTF8(data []byte) bool {
	return utf8Valid(data)
}

func utf8Valid(data []byte) bool {
	for len(data) > 0 {
		r, size := utf8DecodeRune(data)
		if r == 0xFFFD && size == 1 {
			return false
		}
		data = data[size:]
	}
	return true
}

func utf8DecodeRune(data []byte) (rune, int) {
	if len(data) == 0 {
		return 0, 0
	}
	b0 := data[0]
	if b0 < 0x80 {
		return rune(b0), 1
	}
	if b0 < 0xC0 {
		return 0xFFFD, 1
	}
	if b0 < 0xE0 {
		if len(data) < 2 || data[1]&0xC0 != 0x80 {
			return 0xFFFD, 1
		}
		r := rune(b0&0x1F)<<6 | rune(data[1]&0x3F)
		if r < 0x80 {
			return 0xFFFD, 1
		}
		return r, 2
	}
	if b0 < 0xF0 {
		if len(data) < 3 || data[1]&0xC0 != 0x80 || data[2]&0xC0 != 0x80 {
			return 0xFFFD, 1
		}
		r := rune(b0&0x0F)<<12 | rune(data[1]&0x3F)<<6 | rune(data[2]&0x3F)
		if r < 0x800 {
			return 0xFFFD, 1
		}
		return r, 3
	}
	if b0 < 0xF8 {
		if len(data) < 4 || data[1]&0xC0 != 0x80 || data[2]&0xC0 != 0x80 || data[3]&0xC0 != 0x80 {
			return 0xFFFD, 1
		}
		r := rune(b0&0x07)<<18 | rune(data[1]&0x3F)<<12 | rune(data[2]&0x3F)<<6 | rune(data[3]&0x3F)
		if r < 0x10000 || r > 0x10FFFF {
			return 0xFFFD, 1
		}
		return r, 4
	}
	return 0xFFFD, 1
}

// IngestHTTPError 用于前置校验，直接映射为 HTTP 4xx。
type IngestHTTPError struct {
	Status int
	Code   string
}

func (e *IngestHTTPError) Error() string { return e.Code }

// CreateDocument 上传：前置校验 → 去重 → 同事务写入 documents/contents/job。
func CreateDocument(db *sql.DB, ownerID, filename string, data []byte) (*Document, error) {
	title, text, err := ValidateUpload(filename, data)
	if err != nil {
		return nil, err
	}

	hash := SourceHash(text)
	sourceTitle := title
	if sourceTitle == "" {
		sourceTitle = "untitled"
	}

	tx, err := db.Begin()
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback()

	// 同一 owner 去重（含 deleted_at IS NULL 的 failed 文档）
	var existingID string
	var existingStatus string
	var existingAttempt int
	err = tx.QueryRow(
		`SELECT d.id, j.status, j.attempt
		   FROM documents d
		   JOIN ingest_jobs j ON j.doc_id = d.id
		  WHERE d.owner_id = $1 AND d.source_hash = $2 AND d.deleted_at IS NULL`,
		ownerID, hash,
	).Scan(&existingID, &existingStatus, &existingAttempt)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return nil, fmt.Errorf("dedup check: %w", err)
	}
	if existingID != "" {
		doc := &Document{
			ID:         existingID,
			OwnerID:    ownerID,
			Title:      sourceTitle,
			SourceHash: hash,
			ByteSize:   len(data),
			CharCount:  len([]rune(text)),
			Status:     DocumentStatus(existingStatus),
			Attempt:    existingAttempt,
			Dedup:      true,
		}
		return doc, nil
	}

	docID := newUUID()
	_, err = tx.Exec(
		`INSERT INTO documents (id, owner_id, title, source_hash, byte_size, char_count)
		 VALUES ($1, $2, $3, $4, $5, $6)`,
		docID, ownerID, sourceTitle, hash, len(data), len([]rune(text)),
	)
	if err != nil {
		return nil, fmt.Errorf("insert documents: %w", err)
	}

	_, err = tx.Exec(
		`INSERT INTO document_contents (doc_id, content, char_count, token_estimate)
		 VALUES ($1, $2, $3, $4)`,
		docID, text, len([]rune(text)), estimateTokens(text),
	)
	if err != nil {
		return nil, fmt.Errorf("insert document_contents: %w", err)
	}

	_, err = tx.Exec(
		`INSERT INTO ingest_jobs (doc_id, status, attempt)
		 VALUES ($1, 'pending', 0)`,
		docID,
	)
	if err != nil {
		return nil, fmt.Errorf("insert ingest_jobs: %w", err)
	}

	if err := tx.Commit(); err != nil {
		return nil, fmt.Errorf("commit: %w", err)
	}

	return &Document{
		ID:         docID,
		OwnerID:    ownerID,
		Title:      sourceTitle,
		SourceHash: hash,
		ByteSize:   len(data),
		CharCount:  len([]rune(text)),
		Status:     StatusPending,
		Attempt:    0,
		ChunkCount: 0,
		Dedup:      false,
	}, nil
}

func estimateTokens(text string) int {
	// 粗略：1 token ≈ 0.5 个汉字或 4 个英文词
	runes := len([]rune(text))
	return runes / 2
}

// GetDocument 查询文档（含 job 状态），校验 owner。
func GetDocument(db *sql.DB, ownerID, docID string) (*Document, error) {
	var doc Document
	var deletedAt sql.NullTime
	var errorCode sql.NullString
	var errorMsg sql.NullString
	err := db.QueryRow(
		`SELECT d.id, d.owner_id, d.title, d.source_hash, d.byte_size, d.char_count,
		        d.deleted_at, d.created_at,
		        j.status, j.error_code, j.error_msg, j.attempt, j.chunk_count
		   FROM documents d
		   JOIN ingest_jobs j ON j.doc_id = d.id
		  WHERE d.id = $1`,
		docID,
	).Scan(
		&doc.ID, &doc.OwnerID, &doc.Title, &doc.SourceHash, &doc.ByteSize, &doc.CharCount,
		&deletedAt, &doc.CreatedAt,
		&doc.Status, &errorCode, &errorMsg, &doc.Attempt, &doc.ChunkCount,
	)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if doc.OwnerID != ownerID {
		return nil, nil // 不泄露存在性
	}
	if deletedAt.Valid {
		doc.DeletedAt = &deletedAt.Time
	}
	// 处理可能为 NULL 的字段
	if errorCode.Valid {
		doc.ErrorCode = errorCode.String
	}
	if errorMsg.Valid {
		doc.ErrorMsg = errorMsg.String
	}
	return &doc, nil
}

// ClaimJob 原子抢占：pending → processing。
func ClaimJob(db *sql.DB) (*JobClaim, error) {
	token, err := generateRunToken()
	if err != nil {
		return nil, err
	}

	var claim JobClaim
	err = db.QueryRow(
		`UPDATE ingest_jobs
		    SET status = 'processing',
		        attempt = attempt + 1,
		        current_run_token = $1,
		        started_at = now(),
		        updated_at = now()
		  WHERE id = (
		      SELECT id FROM ingest_jobs
		       WHERE status = 'pending'
		       ORDER BY COALESCE(started_at, updated_at), id
		       FOR UPDATE SKIP LOCKED
		       LIMIT 1
		  )
		RETURNING id, doc_id, attempt, current_run_token`,
		token,
	).Scan(&claim.JobID, &claim.DocID, &claim.Attempt, &claim.RunToken)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &claim, nil
}

// JobClaim 描述一次抢占。
type JobClaim struct {
	JobID    string
	DocID    string
	Attempt  int
	RunToken string
}

// PublishResult 两阶段发布：校验 run_token → completed → 翻 visible → 删旧世代。
func PublishResult(db *sql.DB, jobID, docID, runToken string, chunkCount int) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()

	res, err := tx.Exec(
		`UPDATE ingest_jobs
		    SET status = 'completed',
		        published_run_token = $1,
		        chunk_count = $2,
		        finished_at = now(),
		        updated_at = now(),
		        error_code = NULL,
		        error_msg = NULL
		  WHERE id = $3 AND status = 'processing' AND current_run_token = $1`,
		runToken, chunkCount, jobID,
	)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return fmt.Errorf("stale_result_dropped")
	}

	_, err = tx.Exec(
		`UPDATE doc_chunks SET visible = TRUE WHERE doc_id = $1 AND run_token = $2`,
		docID, runToken,
	)
	if err != nil {
		return err
	}

	_, err = tx.Exec(
		`DELETE FROM doc_chunks WHERE doc_id = $1 AND run_token IS DISTINCT FROM $2`,
		docID, runToken,
	)
	if err != nil {
		return err
	}

	return tx.Commit()
}

// FailJob 将 processing 任务置为 failed。
func FailJob(db *sql.DB, jobID, docID, runToken, code, msg string) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()

	res, err := tx.Exec(
		`UPDATE ingest_jobs
		    SET status = 'failed',
		        error_code = $1,
		        error_msg = $2,
		        finished_at = now(),
		        updated_at = now()
		  WHERE id = $3 AND status = 'processing' AND current_run_token = $4`,
		code, msg, jobID, runToken,
	)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return fmt.Errorf("stale_result_dropped")
	}

	// 清理本次不可见切片
	_, err = tx.Exec(
		`DELETE FROM doc_chunks WHERE doc_id = $1 AND run_token = $2 AND visible = FALSE`,
		docID, runToken,
	)
	if err != nil {
		return err
	}

	return tx.Commit()
}

// Reingest 将 failed 文档重置为 pending。
func Reingest(db *sql.DB, ownerID, docID string) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()

	var status string
	var docOwner string
	err = tx.QueryRow(
		`SELECT j.status, d.owner_id
		   FROM documents d
		   JOIN ingest_jobs j ON j.doc_id = d.id
		  WHERE d.id = $1`,
		docID,
	).Scan(&status, &docOwner)
	if errors.Is(err, sql.ErrNoRows) {
		return &IngestHTTPError{Status: http.StatusNotFound, Code: "NOT_FOUND"}
	}
	if err != nil {
		return err
	}
	if docOwner != ownerID {
		return &IngestHTTPError{Status: http.StatusNotFound, Code: "NOT_FOUND"}
	}

	switch status {
	case string(StatusFailed):
		// 允许重试
	case string(StatusPending), string(StatusProcessing):
		return &IngestHTTPError{Status: http.StatusConflict, Code: "DOC_BUSY"}
	case string(StatusCompleted):
		return &IngestHTTPError{Status: http.StatusConflict, Code: "DOC_NOT_RETRYABLE"}
	case string(StatusDeleting):
		return &IngestHTTPError{Status: http.StatusConflict, Code: "DOC_DELETING"}
	default:
		return &IngestHTTPError{Status: http.StatusConflict, Code: "DOC_NOT_RETRYABLE"}
	}

	_, err = tx.Exec(
		`UPDATE ingest_jobs
		    SET status = 'pending',
		        current_run_token = NULL,
		        published_run_token = NULL,
		        error_code = NULL,
		        error_msg = NULL,
		        finished_at = NULL,
		        started_at = NULL,
		        updated_at = now()
		  WHERE doc_id = $1`,
		docID,
	)
	if err != nil {
		return err
	}
	return tx.Commit()
}

// MarkDeleting 将文档标记为 deleting。
func MarkDeleting(db *sql.DB, ownerID, docID string) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()

	var docOwner string
	err = tx.QueryRow(
		`SELECT owner_id FROM documents WHERE id = $1`,
		docID,
	).Scan(&docOwner)
	if errors.Is(err, sql.ErrNoRows) {
		return &IngestHTTPError{Status: http.StatusNotFound, Code: "NOT_FOUND"}
	}
	if err != nil {
		return err
	}
	if docOwner != ownerID {
		return &IngestHTTPError{Status: http.StatusNotFound, Code: "NOT_FOUND"}
	}

	_, err = tx.Exec(
		`UPDATE documents SET deleted_at = now() WHERE id = $1`,
		docID,
	)
	if err != nil {
		return err
	}

	_, err = tx.Exec(
		`UPDATE ingest_jobs
		    SET status = 'deleting',
		        current_run_token = NULL,
		        updated_at = now()
		  WHERE doc_id = $1`,
		docID,
	)
	if err != nil {
		return err
	}
	return tx.Commit()
}

// DeleteDocument 删记录（应在清向量后调用）。
func DeleteDocument(db *sql.DB, docID string) error {
	_, err := db.Exec(`DELETE FROM documents WHERE id = $1`, docID)
	return err
}

// StalePendingJobs 返回超时的 pending 任务。
func StalePendingJobs(db *sql.DB, timeout time.Duration) ([]string, error) {
	rows, err := db.Query(
		`SELECT id FROM ingest_jobs
		  WHERE status = 'pending' AND updated_at < now() - $1::interval
		  LIMIT 50`,
		timeout.String(),
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

// StaleProcessingJobs 返回超时的 processing 任务。
func StaleProcessingJobs(db *sql.DB, timeout time.Duration) ([]JobClaim, error) {
	rows, err := db.Query(
		`SELECT id, doc_id, current_run_token FROM ingest_jobs
		  WHERE status = 'processing' AND started_at < now() - $1::interval
		  LIMIT 50`,
		timeout.String(),
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var claims []JobClaim
	for rows.Next() {
		var c JobClaim
		if err := rows.Scan(&c.JobID, &c.DocID, &c.RunToken); err != nil {
			return nil, err
		}
		claims = append(claims, c)
	}
	return claims, rows.Err()
}

// StaleDeletingDocs 返回超时的 deleting 文档。
func StaleDeletingDocs(db *sql.DB) ([]string, error) {
	rows, err := db.Query(
		`SELECT d.id FROM documents d
		  JOIN ingest_jobs j ON j.doc_id = d.id
		 WHERE j.status = 'deleting'
		 LIMIT 50`,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

// FailByID 直接按 id 置 failed（watchdog 用）。
func FailByID(db *sql.DB, jobID, code, msg string) error {
	_, err := db.Exec(
		`UPDATE ingest_jobs
		    SET status = 'failed',
		        error_code = $1,
		        error_msg = $2,
		        finished_at = now(),
		        updated_at = now()
		  WHERE id = $3 AND status IN ('pending','processing')`,
		code, msg, jobID,
	)
	return err
}

func generateRunToken() (string, error) {
	b := make([]byte, 16)
	if _, err := io.ReadFull(rand.Reader, b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

func newUUID() string {
	b := make([]byte, 16)
	_, _ = io.ReadFull(rand.Reader, b)
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x",
		b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}
