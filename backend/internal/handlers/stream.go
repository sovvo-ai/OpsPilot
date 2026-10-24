// Package handlers —— SSE 流式问答（`POST /api/v1/sessions/{id}/messages/stream`）。
//
// # 契约（PRD §8.5）
//
//	meta → delta* → citation（流后一次性）→ done | error
//
// 上游为 ai-service `/internal/v1/chat/stream`，本层**逐事件透传**，不改写不缓存。
// 关键点：
//   - **S5 同一落库路径**：`done` 事件携带完整 `result`，本层用它与非流式共用的
//     `persistAssistantTurn()` 落库 —— **不存在第二套落库代码**。
//   - **S6 回退**：`Accept` 不含 `text/event-stream` 时直接转非流式（同一份回答内容）。
//   - **S3 禁止静默断流**：任何异常都发 `error` 事件。
//
// # ⚠️ 断连检测（§8.5-S4）—— 4 层坑，改这块前务必读完
//
// 目标：客户端断开 → 停止生成、落 `terminated_reason=interrupted` 的 trace、**不再烧 token**。
// 实测中连续踩了 4 层，只有全部处理才能生效：
//
//  1. **`c.Request.Context()` 取消滞后**。
//     SSE 客户端多为"停止读取"（TCP 半开），Go http server 需读到对方 FIN 才取消 ctx，
//     可能滞后到整轮跑完。→ 本文件起了**心跳 goroutine**（每 200ms 写 SSE 注释行），
//     写失败即判定断连，并 `cancelStream()` 让上游连接**立即关闭**。
//
//  2. **心跳写小数据不报错**。
//     响应通常仅几 KB，**远小于内核发送缓冲**（数十 KB），客户端消失后写入仍会"成功"。
//     → 心跳携带 **8KB 注释填充**（`:xxxx…`；SSE 规范中 `:` 开头为注释，客户端忽略），
//     几次即可填满缓冲，后续写才会触发 EPIPE。
//
//  3. **`streamErr` 形态漏判**。
//     断连时上游读取中断表现为 `*chat.HTTPError{Code:"STREAM_INTERRUPTED"}`，
//     **不是** `context.Canceled`。→ 两种形态**都要**判定为断连。
//
//  4. **分支顺序（最隐蔽）**。
//     断连分支必须位于 `streamErr` 处理**之后** —— 早期写在之前，导致判定时
//     `clientGone` 尚未置位，**断连分支永远不执行**（表现为：日志有"判定断连"，
//     但库里始终没有 interrupted trace）。
//
// 测试侧同类坑：`httpx` 的 `break`/`with` 退出**不保证关闭 TCP**（连接池复用），
// 服务端感知不到。必须用**裸 socket + `SO_LINGER(0)` 强制 RST**，且在**读到 `meta`
// 立即断**（最早时机最易触发 EPIPE）。详见 `scripts/s9_sse_contract.py`。
//
// 验证：`scripts/s9_sse_contract.py`（全新栈 9/9 通过，含断连后 `llm_calls=0`）；
// 证据原文见 `docs/testing/s9-sse-contract.md`。
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
		// ⚠️ 这里必须**区分两种"流中断"**（S9 实测踩到的真缺陷）：
		//
		//   A. **客户端走了**（客户端 RST / 停止读取）→ 记 interrupted trace，不补 error
		//      （对方已经收不到了）。
		//   B. **上游 ai-service 死了**（客户端还在）→ **必须补发 error 事件**：
		//      SSE 响应头早已 200，错误只能靠事件传达；否则前端永远等不到 done/error，
		//      表现为"一直转圈"或"半截答案当完整答案"—— 违反 §8.5-S3「禁止静默断流」。
		//
		// A 的判据：**心跳已探到客户端写失败**（clientGone），或**父 ctx 取消**
		//                （`c.Request.Context()` 就是客户端连接）。
		// B 的判据：streamErr 是中断类错误，但**父 ctx 仍活着** → 说明是我们这一侧
		//                的上游挂了，不是客户端走了。
		//
		// 早期实现把 `STREAM_INTERRUPTED` 一律判为 A，于是 B 场景**静默断流**。
		parentGone := c.Request.Context().Err() != nil
		isInterruptErr := errors.Is(streamErr, context.Canceled) ||
			strings.Contains(streamErr.Error(), "context canceled")
		if he, ok := streamErr.(*chat.HTTPError); ok && he.Code == "STREAM_INTERRUPTED" {
			isInterruptErr = true
		}
		isClientDisconnect := clientGone.Load() || parentGone ||
			(errors.Is(streamErr, context.Canceled) && parentGone)

		if isClientDisconnect {
			clientGone.Store(true)
			log.Printf("判定客户端断连（streamErr=%v, run_id=%s）", streamErr, runID)
		} else {
			// B：上游故障，客户端还在 —— 必须告知，且如实标注为上游不可用
			code, msg := "STREAM_ERROR", "回答流异常中断，请重试"
			if he, ok := streamErr.(*chat.HTTPError); ok {
				code, msg = he.Code, he.Msg
			} else if isInterruptErr {
				code = "AI_SERVICE_UNAVAILABLE"
				msg = "AI 服务中断，请稍后重试（若持续失败请联系管理员并提供 request_id）"
			}
			_ = writeSSE(chat.StreamEvent{Type: "error", Code: code, Message: msg, RequestID: reqID})
			log.Printf("stream error code=%s: %s (request_id=%s)", code, msg, reqID)

			// 上游死了也**不能丢用户的问题**：落一条 interrupted trace（内容为已产出部分）。
			if runID != "" {
				interrupted := &chat.TurnResult{
					RunID: runID, AnswerType: "interrupted",
					TerminatedReason: "upstream_unavailable",
					Degraded:         true,
					DegradeReasons:   []string{"ai_service_unavailable"},
					FinalText:        doneResultText(doneResult),
					DurationMS:       int(time.Since(startedAt).Milliseconds()),
				}
				if _, _, perr := persistAssistantTurn(db, sessionID, ownerID, reqID, interrupted); perr != nil {
					log.Printf("落上游中断 trace 失败: %v (run_id=%s)", perr, runID)
				} else {
					log.Printf("上游中断：已落 trace (run_id=%s)", runID)
				}
			}
			return
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
				Message:   "回答已生成但保存失败，请刷新重试",
				RequestID: reqID,
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

// doneResultText 安全取 done 结果的正文（可能为 nil）。
func doneResultText(r *chat.TurnResult) string {
	if r == nil {
		return ""
	}
	return r.FinalText
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
