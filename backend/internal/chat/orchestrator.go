package chat

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"time"
)

// TurnRequest 是发给 ai-service 的编排请求（对齐 openapi-ai TurnRequest）。
type TurnRequest struct {
	RequestID string           `json:"request_id"`
	SessionID string           `json:"session_id"`
	TurnIndex int              `json:"turn_index"`
	Question  string           `json:"question"`
	History   []HistoryMessage `json:"history,omitempty"`
	AttachLog string           `json:"attach_log,omitempty"`
	OwnerID   string           `json:"owner_id"` // 内部扩展：检索过滤所需
}

// TurnResult 是 ai-service 返回的编排结果（对齐 openapi-ai TurnResult）。
type TurnResult struct {
	RunID            string         `json:"run_id"`
	SessionID        string         `json:"session_id"`
	Route            string         `json:"route"`
	FinalText        string         `json:"final_text"`
	AnswerType       string         `json:"answer_type"`
	Citations        []Citation     `json:"citations"`
	Degraded         bool           `json:"degraded"`
	DegradeReasons   []string       `json:"degrade_reasons"`
	Preempted        []string       `json:"preempted"`
	TerminatedReason string         `json:"terminated_reason"`
	Usage            map[string]int `json:"usage"`
	DurationMS       int            `json:"duration_ms"`
	// --- S6 轨迹（Go 落 run_steps）---
	NodePath []string   `json:"node_path"`
	Steps    []StepInfo `json:"steps"`
	// --- S7 工具审计（Go 落 tool_calls）---
	ToolCallRecords []ToolCallRecord `json:"tool_call_records"`
	// --- 检索统计（可选）---
	RetrievalTopScore float64 `json:"retrieval_top_score"`
	RetrievedCount    int     `json:"retrieved_count"`
	RewrittenQuery    *string `json:"rewritten_query"`
}

// StepInfo 是单节点轨迹（对齐 run_steps 表）。
type StepInfo struct {
	Node          string `json:"node"`
	DurationMS    int    `json:"duration_ms"`
	InputDigest   string `json:"input_digest"`
	OutputSummary string `json:"output_summary"`
	Degraded      bool   `json:"degraded"`
}

// ToolCallRecord 是工具审计行（对齐 tool_calls 表，S7）。
// 只含摘要：不落原始入参（日志可能含敏感信息，PRD NFR-S5）。
type ToolCallRecord struct {
	ToolName      string `json:"tool_name"`
	ArgsDigest    string `json:"args_digest"`
	Status        string `json:"status"`
	DurationMS    int    `json:"duration_ms"`
	OutputSummary string `json:"output_summary"`
}

// Orchestrator 负责调用 ai-service 的编排接口。
type Orchestrator struct {
	baseURL string
	token   string
	client  *http.Client
}

// NewOrchestrator 创建编排客户端。与 ingest 一致：显式禁用环境代理。
func NewOrchestrator(baseURL, token string) *Orchestrator {
	return &Orchestrator{
		baseURL: baseURL,
		token:   token,
		client: &http.Client{
			Timeout:   60 * time.Second,
			Transport: &http.Transport{Proxy: nil},
		},
	}
}

// RunTurn 调用 /internal/v1/chat，返回结构化结果。
func (o *Orchestrator) RunTurn(req TurnRequest) (*TurnResult, error) {
	body, err := json.Marshal(req)
	if err != nil {
		return nil, err
	}
	httpReq, err := http.NewRequest(http.MethodPost, o.baseURL+"/internal/v1/chat", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("X-Internal-Token", o.token)
	httpReq.Header.Set("X-Request-Id", req.RequestID)

	resp, err := o.client.Do(httpReq)
	if err != nil {
		return nil, &HTTPError{Status: http.StatusServiceUnavailable, Code: "AI_SERVICE_UNAVAILABLE", Msg: err.Error()}
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		var errBody struct {
			Error struct {
				Code    string `json:"code"`
				Message string `json:"message"`
			} `json:"error"`
		}
		_ = json.NewDecoder(resp.Body).Decode(&errBody)
		code := errBody.Error.Code
		if code == "" {
			code = "AI_SERVICE_ERROR"
		}
		return nil, &HTTPError{
			Status: http.StatusBadGateway,
			Code:   code,
			Msg:    fmt.Sprintf("ai-service 返回 %d: %s", resp.StatusCode, errBody.Error.Message),
		}
	}

	var result TurnResult
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("解析编排结果失败: %w", err)
	}
	return &result, nil
}
