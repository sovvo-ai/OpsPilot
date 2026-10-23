package ingest

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"time"
)

const (
	pendingTimeout    = 2 * time.Minute
	processingTimeout = 150 * time.Second
	deletingInterval  = 60 * time.Second
	claimInterval     = 2 * time.Second
)

// Dispatcher 负责后台摄取任务派发与 watchdog。
type Dispatcher struct {
	db            *sql.DB
	aiServiceURL  string
	aiServiceToken string
	stop          chan struct{}
}

// NewDispatcher 创建派发器。
func NewDispatcher(db *sql.DB, aiServiceURL, aiServiceToken string) *Dispatcher {
	return &Dispatcher{
		db:             db,
		aiServiceURL:   aiServiceURL,
		aiServiceToken: aiServiceToken,
		stop:           make(chan struct{}),
	}
}

// Start 启动后台 goroutine（claim + watchdog）。
func (d *Dispatcher) Start() {
	// 启动后立即跑一轮 watchdog，收敛重启前的在途任务
	go d.runWatchdog(true)
	go d.runClaimer()
	go d.runPeriodicWatchdog()
}

// Stop 停止后台 goroutine。
func (d *Dispatcher) Stop() {
	close(d.stop)
}

func (d *Dispatcher) runClaimer() {
	ticker := time.NewTicker(claimInterval)
	defer ticker.Stop()
	for {
		select {
		case <-d.stop:
			return
		case <-ticker.C:
			d.claimAndRun()
		}
	}
}

func (d *Dispatcher) claimAndRun() {
	if d.db == nil {
		return
	}
	for {
		claim, err := ClaimJob(d.db)
		if err != nil {
			log.Printf("claim job error: %v", err)
			return
		}
		if claim == nil {
			return
		}
		go d.runIngest(claim)
	}
}

func (d *Dispatcher) runIngest(claim *JobClaim) {
	start := time.Now()
	payload := map[string]any{
		"doc_id":    claim.DocID,
		"run_token": claim.RunToken,
		"attempt":   claim.Attempt,
	}
	body, _ := json.Marshal(payload)

	client := &http.Client{
		Timeout:   120 * time.Second,
		Transport: &http.Transport{Proxy: nil},
	}
	url := d.aiServiceURL + "/internal/v1/ingest"
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		_ = FailJob(d.db, claim.JobID, claim.DocID, claim.RunToken, "EMBEDDING_FAILED", err.Error())
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Internal-Token", d.aiServiceToken)

	resp, err := client.Do(req)
	if err != nil {
		_ = FailJob(d.db, claim.JobID, claim.DocID, claim.RunToken, "EMBEDDING_FAILED", err.Error())
		return
	}
	defer resp.Body.Close()

	var result struct {
		RunToken   string `json:"run_token"`
		Status     string `json:"status"`
		ChunkCount int    `json:"chunk_count"`
		ErrorCode  string `json:"error_code"`
		ErrorMsg   string `json:"error_msg"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		_ = FailJob(d.db, claim.JobID, claim.DocID, claim.RunToken, "EMBEDDING_FAILED", err.Error())
		return
	}
	elapsed := time.Since(start).Seconds()
	log.Printf("ingest done doc_id=%s elapsed=%.1fs status=%s chunks=%d",
		claim.DocID, elapsed, result.Status, result.ChunkCount)

	if result.Status != "ok" || result.ChunkCount == 0 {
		code := result.ErrorCode
		if code == "" {
			code = "EMBEDDING_FAILED"
		}
		// 只有 Python 显式返回 ok 但 chunk_count=0 时才按 EMPTY_CONTENT 处理；
		// 错误响应（如 EMBEDDING_FAILED）应保留原错误码，避免掩盖网络/模型问题。
		if result.Status == "ok" && result.ChunkCount == 0 && code != "EMPTY_CONTENT" {
			code = "EMPTY_CONTENT"
		}
		_ = FailJob(d.db, claim.JobID, claim.DocID, claim.RunToken, code, result.ErrorMsg)
		return
	}

	if err := PublishResult(d.db, claim.JobID, claim.DocID, claim.RunToken, result.ChunkCount); err != nil {
		log.Printf("publish result dropped: %v", err)
	}
}

func (d *Dispatcher) runWatchdog(immediate bool) {
	if !immediate {
		select {
		case <-d.stop:
			return
		case <-time.After(60 * time.Second):
		}
	}
	if d.db == nil {
		return
	}

	// pending 超时
	ids, err := StalePendingJobs(d.db, pendingTimeout)
	if err != nil {
		log.Printf("watchdog stale pending: %v", err)
	} else {
		for _, id := range ids {
			if err := FailByID(d.db, id, "DISPATCH_FAILED", "pending 超时未被抢占"); err != nil {
				log.Printf("watchdog fail pending %s: %v", id, err)
			}
		}
	}

	// processing 超时
	claims, err := StaleProcessingJobs(d.db, processingTimeout)
	if err != nil {
		log.Printf("watchdog stale processing: %v", err)
	} else {
		for _, c := range claims {
			if err := FailJob(d.db, c.JobID, c.DocID, c.RunToken, "TIMEOUT", "processing 超时"); err != nil {
				log.Printf("watchdog fail processing %s: %v", c.JobID, err)
			}
		}
	}

	// deleting 清理
	docs, err := StaleDeletingDocs(d.db)
	if err != nil {
		log.Printf("watchdog deleting scan: %v", err)
	} else {
		for _, docID := range docs {
			d.cleanupDeleting(docID)
		}
	}
}

func (d *Dispatcher) runPeriodicWatchdog() {
	for {
		select {
		case <-d.stop:
			return
		case <-time.After(deletingInterval):
			d.runWatchdog(true)
		}
	}
}

func (d *Dispatcher) cleanupDeleting(docID string) {
	// 调 Python 删向量
	client := &http.Client{Timeout: 30 * time.Second, Transport: &http.Transport{Proxy: nil}}
	url := fmt.Sprintf("%s/internal/v1/documents/%s/vectors", d.aiServiceURL, docID)
	req, err := http.NewRequest(http.MethodDelete, url, nil)
	if err != nil {
		return
	}
	req.Header.Set("X-Internal-Token", d.aiServiceToken)
	resp, err := client.Do(req)
	if err != nil {
		log.Printf("cleanup vectors %s: %v", docID, err)
		return
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		log.Printf("cleanup vectors %s status %d", docID, resp.StatusCode)
		return
	}

	// 删三表记录
	_, err = d.db.Exec(`DELETE FROM documents WHERE id = $1`, docID)
	if err != nil {
		log.Printf("cleanup documents %s: %v", docID, err)
	}
}
