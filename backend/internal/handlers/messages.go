package handlers

import (
	"database/sql"
	"encoding/json"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
	"opspilot/backend/internal/chat"
	"opspilot/backend/internal/middleware"
)

// 最近 4 轮 = 8 条消息（PRD FR-31 / agent-workflow TurnRequest.history maxItems=8）
const historyMessageLimit = 8

// AskMessage 提问（非流式，S5）。

// 流程（用户指定）：
//  1. 校验会话归属、问题合法性
//  2. 组装最近 4 轮（8 条）上下文
//  3. 落 user 消息
//  4. 调 ai-service /internal/v1/chat（retrieval→prompt→生成→引用提取/拒答）
//  5. 引用校验（代码层强制：只保留属于本次检索集的 chunk_id）
//  6. 落 assistant 消息（citations JSONB）+ agent_runs
func AskMessage(c *gin.Context) {
	ownerID := ownerOf(c)
	if ownerID == "" {
		middleware.AbortWithError(c, http.StatusUnauthorized, "UNAUTHORIZED", "无法识别用户")
		return
	}

	var body struct {
		Question  string `json:"question"`
		AttachLog string `json:"attach_log"`
	}
	if err := c.ShouldBindJSON(&body); err != nil {
		middleware.AbortWithError(c, http.StatusUnprocessableEntity, "INVALID_REQUEST", "请求体格式错误")
		return
	}
	question := strings.TrimSpace(body.Question)
	if question == "" {
		middleware.AbortWithError(c, http.StatusUnprocessableEntity, "INVALID_QUESTION", "问题不能为空")
		return
	}
	if len([]rune(question)) > 500 {
		middleware.AbortWithError(c, http.StatusUnprocessableEntity, "INVALID_QUESTION", "问题超过 500 字")
		return
	}
	if len(body.AttachLog) > 8192 {
		middleware.AbortWithError(c, http.StatusUnprocessableEntity, "INVALID_REQUEST", "日志超过 8KB")
		return
	}

	sessionID := c.Param("session_id")
	db := c.MustGet("db").(*sql.DB)

	// 1. 会话归属校验（跨用户 → 404）
	if _, err := chat.GetSession(db, ownerID, sessionID); err != nil {
		handleChatErr(c, err)
		return
	}

	// 2. 组装最近 4 轮上下文 + turn_index
	history, err := chat.RecentHistory(db, sessionID, historyMessageLimit)
	if err != nil {
		handleChatErr(c, err)
		return
	}
	turnIndex, err := chat.CountTurns(db, sessionID)
	if err != nil {
		handleChatErr(c, err)
		return
	}

	// 3. 落 user 消息
	userMsgID, err := chat.InsertMessage(db, sessionID, "user", question, "[]", false)
	if err != nil {
		handleChatErr(c, err)
		return
	}
	_ = userMsgID

	// 4. 调编排
	reqID, _ := c.Get("request_id")
	reqIDStr, _ := reqID.(string)
	orch := c.MustGet("orchestrator").(*chat.Orchestrator)

	result, err := orch.RunTurn(chat.TurnRequest{
		RequestID: reqIDStr,
		SessionID: sessionID,
		TurnIndex: turnIndex,
		Question:  question,
		History:   history,
		AttachLog: body.AttachLog,
		OwnerID:   ownerID,
	})
	if err != nil {
		// 编排不可用：把错误如实返回，但不留半截落库（user 消息已落，可重试）
		handleChatErr(c, err)
		return
	}

	// 5. 引用落库前再做一次代码层过滤：chunk_id 必须形似 UUID 且非空
	citations := make([]chat.Citation, 0, len(result.Citations))
	for _, cit := range result.Citations {
		if cit.ChunkID == "" || cit.DocID == "" {
			continue
		}
		citations = append(citations, cit)
	}
	citJSON, _ := json.Marshal(citations)

	// 6. 落 assistant 消息 + 完整轨迹（agent_runs + run_steps）
	tokens := func(k string) int {
		if result.Usage == nil {
			return 0
		}
		return result.Usage[k]
	}
	trace := chat.RunTrace{
		RunID:             result.RunID,
		SessionID:         sessionID,
		OwnerID:           ownerID,
		RequestID:         reqIDStr,
		Route:             result.Route,
		NodePath:          result.NodePath,
		Steps:             result.Steps,
		ToolCalls:         result.ToolCallRecords,
		RetrievedChunkIDs: result.RetrievedChunkIDs,
		LLMCalls:          tokens("llm_calls"),
		ToolCallsCount:    tokens("tool_calls"),
		PromptTokens:      tokens("prompt_tokens"),
		CompletionTokens:  tokens("completion_tokens"),
		Degraded:          result.Degraded,
		DegradeReasons:    result.DegradeReasons,
		Preempted:         result.Preempted,
		TerminatedReason:  result.TerminatedReason,
		DurationMS:        result.DurationMS,
	}
	msgID, err := chat.InsertAssistantWithRun(db, trace, result.FinalText, string(citJSON))
	if err != nil {
		handleChatErr(c, err)
		return
	}
	_ = chat.TouchSession(db, sessionID)

	c.JSON(http.StatusOK, gin.H{
		"message": gin.H{
			"id":          msgID,
			"session_id":  sessionID,
			"role":        "assistant",
			"content":     result.FinalText,
			"citations":   citations,
			"answer_type": result.AnswerType,
			"degraded":    result.Degraded,
			"run_id":      result.RunID,
		},
		"run_id":          result.RunID,
		"degraded":        result.Degraded,
		"degrade_reasons": result.DegradeReasons,
	})
}
