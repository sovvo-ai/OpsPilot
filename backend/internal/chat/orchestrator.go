package chat

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
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
	// S8：本次检索的完整 Top-K 集合（评测算 Recall@5/MRR@5 必需）
	RetrievedChunkIDs []string `json:"retrieved_chunk_ids"`
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

// RunTurnStream 调用 /internal/v1/chat/stream 并**逐事件透传**（S9）。
//
// 设计要点（PRD §8.5）：
// - 与 RunTurn **共用同一 ai-service 入口的编排逻辑**（§8.5-S5）；
//   Go 侧只做代理与落库，不复制业务。
// - `onEvent` 每收到一个 SSE 事件调用一次；**返回值是 run_turn 的最终结果**，
//   由 `done` 事件的 usage + 后续 `GET /runs` 不可得，故这里从 `done`/`meta`
//   采集 run_id，并在流结束后**由调用方查库**补全（同一落库路径）。
// - **禁止静默断流**（§8.5-S3）：上游异常/连接中断都返回 error 供上层发 `error` 事件。
type StreamEvent struct {
	Type      string          `json:"type"`
	RunID     string          `json:"run_id,omitempty"`
	SessionID string          `json:"session_id,omitempty"`
	RequestID string          `json:"request_id,omitempty"`
	Text      string          `json:"text,omitempty"`
	Citations json.RawMessage `json:"citations,omitempty"`
	Usage     map[string]int  `json:"usage,omitempty"`
	Code      string          `json:"code,omitempty"`
	Message   string          `json:"message,omitempty"`
	// Result：`done` 事件携带的**完整 TurnResult** —— Go 侧据此走与非流式
	// 完全相同的落库路径（§8.5-S5），无需二次查询。
	Result *TurnResult `json:"result,omitempty"`
}

// RunTurnStream 流式执行一轮；`onEvent` 透传每个事件。
//
// `ctx` 用于**断连锁链**（PRD §8.5-S4）：ctx 取消（客户端断开）时**立即关闭上游连接**，
// 使 ai-service 感知断连并停止生成 —— 不能等 `RunTurnStream` 自然返回才关
// （那时整轮已跑完、token 已烧掉）。
//
// 返回采集到的 runID（来自 meta 事件）与 error。
func (o *Orchestrator) RunTurnStream(
	ctx context.Context,
	req TurnRequest,
	onEvent func(StreamEvent) error,
) (runID string, err error) {
	body, err := json.Marshal(req)
	if err != nil {
		return "", err
	}
	httpReq, err := http.NewRequestWithContext(
		ctx, http.MethodPost, o.baseURL+"/internal/v1/chat/stream", bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("X-Internal-Token", o.token)
	httpReq.Header.Set("X-Request-Id", req.RequestID)
	httpReq.Header.Set("Accept", "text/event-stream")

	// 流式用**无总超时**的 client（长回答可能 >60s；靠 ctx 与上游超时控制）
	streamClient := &http.Client{
		Transport: &http.Transport{Proxy: nil},
	}
	resp, err := streamClient.Do(httpReq)
	if err != nil {
		return "", &HTTPError{
			Status: http.StatusServiceUnavailable,
			Code:   "AI_SERVICE_UNAVAILABLE",
			Msg:    err.Error(),
		}
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
		return "", &HTTPError{
			Status: http.StatusBadGateway,
			Code:   code,
			Msg:    fmt.Sprintf("ai-service stream 返回 %d: %s", resp.StatusCode, errBody.Error.Message),
		}
	}

	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	var eventType string
	for scanner.Scan() {
		line := scanner.Text()
		switch {
		case strings.HasPrefix(line, "event:"):
			eventType = strings.TrimSpace(strings.TrimPrefix(line, "event:"))
		case strings.HasPrefix(line, "data:"):
			raw := strings.TrimSpace(strings.TrimPrefix(line, "data:"))
			if raw == "" {
				continue
			}
			var ev StreamEvent
			if jerr := json.Unmarshal([]byte(raw), &ev); jerr != nil {
				// 单条事件解析失败不应中断整条流 —— 但必须让上层知道（不允许静默）
				_ = onEvent(StreamEvent{
					Type: "error", Code: "STREAM_PARSE_ERROR",
					Message: "事件解析失败: " + jerr.Error(),
				})
				continue
			}
			if ev.Type == "" {
				ev.Type = eventType
			}
			if ev.Type == "meta" && ev.RunID != "" {
				runID = ev.RunID
			}
			if err := onEvent(ev); err != nil {
				return runID, err // 通常是客户端断连
			}
		}
	}
	if err := scanner.Err(); err != nil {
		return runID, &HTTPError{
			Status: http.StatusBadGateway,
			Code:   "STREAM_INTERRUPTED",
			Msg:    "流读取中断: " + err.Error(),
		}
	}
	return runID, nil
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
