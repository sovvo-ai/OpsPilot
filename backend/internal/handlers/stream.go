package handlers

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"sync/atomic"
	"time"

	"github.com/gin-gonic/gin"
	"opspilot/backend/internal/chat"
	"opspilot/backend/internal/middleware"
)

// AskMessageStream 提问（SSE 流式，S9）。
//
// PRD §8.5 六条契约的落地点：
//   - S1 事件：`meta` / `delta` / `citation`（流后一次性）/ `done` / `error` —— 由 ai-service 产出，
//     Go 侧**逐事件透传**（不改写、不缓存）。
//   - S2 正常结束：`done` 必发（ai-service 保证）；Go 侧转发后**才**落库。
//   - S3 错误结束：任何异常都发 `error` 事件（含 code），**禁止静默断流**。
//   - S4 客户端断开：`c.Request.Context()` 取消 → 关闭上游连接 → ai-service 停止生成
//     并落 `interrupted` trace。
//   - S5 与非流式兼容：复用 `persistAssistantTurn`（同一落库路径），结果来自 `done.result`。
//   - S6 客户端不支持流：`Accept` 不含 `text/event-stream` → **回退为非流式**（同一份回答内容）。
//
// 流程：校验（同非流式）→ 落 user 消息 → 流式调用 → 边收边写 SSE → done 后落库。
func AskMessageStream(c *gin.Context) {
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

	// S6：客户端不支持流 → 回退非流式（同一份回答内容）。
	// 判据：显式声明不接受 text/event-stream。
	if !acceptsEventStream(c) {
		askNonStreamingFallback(c, body.Question, body.AttachLog)
		return
	}

	sessionID := c.Param("session_id")
	db := c.MustGet("db").(*sql.DB)

	if _, err := chat.GetSession(db, ownerID, sessionID); err != nil {
		handleChatErr(c, err)
		return
	}
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

	// 落 user 消息（与非流式一致）
	if _, err := chat.InsertMessage(db, sessionID, "user", question, "[]", false); err != nil {
		handleChatErr(c, err)
		return
	}

	reqIDAny, _ := c.Get("request_id")
	reqID, _ := reqIDAny.(string)
	orch := c.MustGet("orchestrator").(*chat.Orchestrator)

	startedAt := time.Now()

	// —— SSE 响应头 ——
	h := c.Writer.Header()
	h.Set("Content-Type", "text/event-stream; charset=utf-8")
	h.Set("Cache-Control", "no-cache")
	h.Set("Connection", "keep-alive")
	h.Set("X-Accel-Buffering", "no") // 防中间层缓冲
	c.Writer.WriteHeader(http.StatusOK)
	c.Writer.Flush()

	// 断连标志（心跳 goroutine 与主流程共享，用 atomic 避免竞态）
	var clientGone atomic.Bool

	// 上游 ctx：断连时立即取消，关闭到 ai-service 的连接（§8.5-S4 的"停止生成"）
	streamCtx, cancelStream := context.WithCancel(c.Request.Context())
	defer cancelStream()

	// —— 断连探测：**带填充的心跳** + ctx 双路 ——
	//
	// **为什么需要填充**：SSE 响应通常只有几 KB，**远小于内核发送缓冲**（默认数十 KB），
	// 客户端消失后服务端写小数据仍会"成功"（被缓冲吸收），根本感知不到断连。
	// 实测：3.8s 的回答 + 300ms 心跳 ×14 次**全部写成功**，`clientGone` 始终 false。
	// 故心跳携带 **8KB 注释填充**，几次即可填满缓冲 → 后续写触发 EPIPE/RST → 判定断连。
	// 这是**观测性**手段：填充内容是 SSE 注释（`:` 开头），客户端会忽略，不影响契约。
	const hbPad = 8192
	pad := strings.Repeat("x", hbPad)
	heartbeatDone := make(chan struct{})
	hbCount := 0
	go func() {
		tk := time.NewTicker(200 * time.Millisecond)
		defer tk.Stop()
		for {
			select {
			case <-heartbeatDone:
				return
			case <-streamCtx.Done():
				return
			case <-tk.C:
				hbCount++
				// SSE 注释行 + 填充（客户端忽略 `:` 开头行）
				if _, err := fmt.Fprintf(c.Writer, ":%s\n\n", pad); err != nil {
					log.Printf("[hb] 写心跳失败（第 %d 次）→ 判定断连: %v", hbCount, err)
					clientGone.Store(true)
					cancelStream() // 断连锁链：上游连接随 ctx 立即关闭
					return
				}
				c.Writer.Flush()
			}
		}
	}()
	defer close(heartbeatDone)

	var (
		doneResult *chat.TurnResult
		runID      string
	)

	writeSSE := func(ev chat.StreamEvent) error {
		payload, _ := json.Marshal(ev)
		if _, err := fmt.Fprintf(c.Writer, "event: %s\ndata: %s\n\n", ev.Type, payload); err != nil {
			return err
		}
		c.Writer.Flush()
		return nil
	}

	// onEvent：透传事件；到 done 时记下完整结果（用于落库）。
	onEvent := func(ev chat.StreamEvent) error {
		// 客户端断开探测（S4）：
		//   ① ctx 取消（Gin 感知到连接关闭）
		//   ② **写入失败**（客户端已走，Flush 报错）
		//   仅靠 ① 不够：内核 socket 关闭后 ctx 取消可能滞后于写入失败，故两者都判。
		select {
		case <-c.Request.Context().Done():
			clientGone.Store(true)
			return c.Request.Context().Err()
		default:
		}

		if ev.RunID != "" && runID == "" {
			runID = ev.RunID // meta 事件里的 run_id（中断时也用它落 trace）
		}
		if ev.Type == "done" && ev.Result != nil {
			doneResult = ev.Result
		}
		if werr := writeSSE(ev); werr != nil {
			clientGone.Store(true)
			log.Printf("SSE 写入失败（客户端已断开）: %v", werr)
			return werr
		}
		return nil
	}

	_, streamErr := orch.RunTurnStream(streamCtx, chat.TurnRequest{
		RequestID: reqID,
		SessionID: sessionID,
		TurnIndex: turnIndex,
		Question:  question,
		History:   history,
		AttachLog: body.AttachLog,
		OwnerID:   ownerID,
	}, onEvent)

	// 上游/内部错误 → 必发 error（S3：禁止静默断流）
	if streamErr != nil && !clientGone.Load() {
		// **上游连接被切断 = 客户端断连**（§8.5-S4）。
		// 两种表现形式（实测都出现过）：
		//   ① `context.Canceled`：ctx 取消（心跳探测到写失败后 cancelStream）
		//   ② `STREAM_INTERRUPTED`：上游 resp.Body 读取中断（ctx 取消 → transport 关闭连接）
		// 早期只判 ①，漏了 ②，导致断流被误报为普通错误且不落 interrupted trace。
		isDisconnect := errors.Is(streamErr, context.Canceled) ||
			strings.Contains(streamErr.Error(), "context canceled")
		if he, ok := streamErr.(*chat.HTTPError); ok && he.Code == "STREAM_INTERRUPTED" {
			isDisconnect = true
		}
		if isDisconnect {
			clientGone.Store(true)
			log.Printf("判定客户端断连（streamErr=%v, run_id=%s）", streamErr, runID)
		} else {
			code, msg := "STREAM_ERROR", streamErr.Error()
			if he, ok := streamErr.(*chat.HTTPError); ok {
				code, msg = he.Code, he.Msg
			}
			_ = writeSSE(chat.StreamEvent{Type: "error", Code: code, Message: msg})
			log.Printf("stream error code=%s: %s (request_id=%s)", code, msg, reqID)
		}
	}

	// —— 客户端断连（§8.5-S4）——
	// 断连时上游连接已随 ctx 关闭（ai-service 会察觉并停止生成），但 **trace 必须落下**：
	// 用已知 run_id 补一条 `interrupted` 的 trace，使"断流后 trace 标记 interrupted"
	// 可被 SQL 断言（U9 要求）。**注意**：即使 done 已到达（生成太快来不及中断），
	// 只要检测到断连，就按 interrupted 记账 —— 因为客户端并未完整收到回答。
	if clientGone.Load() && runID != "" {
		interrupted := &chat.TurnResult{
			RunID:            runID,
			Route:            "",
			FinalText:        "",
			AnswerType:       "interrupted",
			TerminatedReason: "interrupted",
			Degraded:         true,
			DegradeReasons:   []string{"client_disconnected"},
			DurationMS:       int(time.Since(startedAt).Milliseconds()),
		}
		if doneResult != nil {
			// 保留已产出的内容与轨迹信息，仅把终止原因改为 interrupted
			interrupted.Route = doneResult.Route
			interrupted.NodePath = doneResult.NodePath
			interrupted.Steps = doneResult.Steps
			interrupted.ToolCallRecords = doneResult.ToolCallRecords
			interrupted.RetrievedChunkIDs = doneResult.RetrievedChunkIDs
			interrupted.RetrievalTopScore = doneResult.RetrievalTopScore
			interrupted.RewrittenQuery = doneResult.RewrittenQuery
			interrupted.Preempted = doneResult.Preempted
			interrupted.DurationMS = doneResult.DurationMS
			interrupted.FinalText = doneResult.FinalText
			interrupted.Usage = doneResult.Usage // 保留已消耗的计费（如实记录）
			interrupted.Citations = doneResult.Citations
		}
		if _, _, perr := persistAssistantTurn(db, sessionID, ownerID, reqID, interrupted); perr != nil {
			log.Printf("落 interrupted trace 失败: %v (run_id=%s)", perr, runID)
		} else {
			log.Printf("客户端断连：已落 interrupted trace (run_id=%s)", runID)
		}
		return
	}

	// S5：done 之后走**与非流式相同的落库路径**
	if doneResult != nil {
		msgID, citations, perr := persistAssistantTurn(db, sessionID, ownerID, reqID, doneResult)
		if perr != nil {
			// 落库失败：客户端已收到全文，补发 error 说明"结果未保存"
			_ = writeSSE(chat.StreamEvent{
				Type: "error", Code: "PERSIST_FAILED",
				Message: "回答已生成但保存失败，请刷新重试",
			})
			log.Printf("落库失败（流式）: %v (request_id=%s)", perr, reqID)
			return
		}
		// 落库元信息以 `saved` 事件补发（前端可据此刷新历史列表；不属 §8.5 必需事件，
		// 但**不新增语义**：仅是落库成功的确认，前端可忽略）
		_ = writeSSE(chat.StreamEvent{
			Type: "saved", RunID: doneResult.RunID,
			Message: msgID,
		})
		_ = citations
	}

	// 无 done 且非客户端断开 → 说明上游提前结束（异常），上面已补 error
	_ = io.EOF
}

// acceptsEventStream 判断客户端是否接受 SSE（S6 回退判据）。
//
// 规则：**显式声明** `text/event-stream` 才走流式；
// 未声明或是 `application/json` → 回退非流式（"客户端不支持流"）。
func acceptsEventStream(c *gin.Context) bool {
	accept := c.GetHeader("Accept")
	if accept == "" {
		return false // 未声明 → 按不支持处理（保守，保证内容完整）
	}
	return strings.Contains(accept, "text/event-stream")
}

// askNonStreamingFallback 回退为非流式（S6）：直接复用非流式 handler 的逻辑。
//
// **为什么直接调 AskMessage**：S6 要求"回退为非流式一次性返回（**同一份回答内容**）"，
// 复用同一 handler 是保证内容一致的最简方式（也避免第二套编排/落库代码）。
func askNonStreamingFallback(c *gin.Context, question, attachLog string) {
	// 重放请求体，供 AskMessage 再次绑定
	c.Request.Body = io.NopCloser(strings.NewReader(
		fmt.Sprintf(`{"question":%s,"attach_log":%s}`,
			jsonString(question), jsonString(attachLog)),
	))
	AskMessage(c)
}

// jsonString 把字符串安全编码为 JSON 字面量。
func jsonString(s string) string {
	b, _ := json.Marshal(s)
	return string(b)
}

// tokenOf 读取 TurnResult.Usage 里的计数值（缺失返回 0）。
func tokenOf(r *chat.TurnResult, key string) int {
	if r == nil || r.Usage == nil {
		return 0
	}
	return r.Usage[key]
}
