package chat

import (
	"encoding/json"
	"testing"
)

func TestCitationJSONRoundTrip(t *testing.T) {
	raw := `[
      {"chunk_id":"11111111-1111-1111-1111-111111111111",
       "doc_id":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
       "doc_title":"k8s-pod-troubleshooting.md",
       "snippet":"CrashLoopBackOff 表示容器反复退出",
       "score":0.83,"source_type":"kb","status":"live"}
    ]`
	var cits []Citation
	if err := json.Unmarshal([]byte(raw), &cits); err != nil {
		t.Fatalf("解析引用 JSON 失败: %v", err)
	}
	if len(cits) != 1 {
		t.Fatalf("期望 1 条引用，实际 %d", len(cits))
	}
	c := cits[0]
	if c.ChunkID != "11111111-1111-1111-1111-111111111111" {
		t.Fatalf("chunk_id 解析错误: %s", c.ChunkID)
	}
	if c.SourceType != "kb" {
		t.Fatalf("source_type 应为 kb，实际 %s", c.SourceType)
	}
	if c.Score != 0.83 {
		t.Fatalf("score 解析错误: %v", c.Score)
	}
}

func TestTurnResultFields(t *testing.T) {
	raw := `{
      "run_id":"22222222-2222-2222-2222-222222222222",
      "session_id":"33333333-3333-3333-3333-333333333333",
      "route":"kb_qa",
      "final_text":"回答内容[1]",
      "answer_type":"answered",
      "citations":[],
      "degraded":false,
      "degrade_reasons":[],
      "terminated_reason":"completed",
      "usage":{"llm_calls":1,"tool_calls":0},
      "duration_ms":1234
    }`
	var r TurnResult
	if err := json.Unmarshal([]byte(raw), &r); err != nil {
		t.Fatalf("解析 TurnResult 失败: %v", err)
	}
	if r.Route != "kb_qa" || r.AnswerType != "answered" {
		t.Fatalf("route/answer_type 解析错误: %s/%s", r.Route, r.AnswerType)
	}
	if r.TerminatedReason != "completed" {
		t.Fatalf("terminated_reason 解析错误: %s", r.TerminatedReason)
	}
	if r.Usage["llm_calls"] != 1 {
		t.Fatalf("usage.llm_calls 解析错误: %v", r.Usage["llm_calls"])
	}
}

func TestTurnRequestMarshalOmitsEmpty(t *testing.T) {
	req := TurnRequest{
		RequestID: "req-1",
		SessionID: "s-1",
		Question:  "问题",
		OwnerID:   "demo-user",
	}
	b, err := json.Marshal(req)
	if err != nil {
		t.Fatalf("序列化失败: %v", err)
	}
	s := string(b)
	// 空 history/attach_log 必须被省略（omitempty），避免下游按存在与否分支
	if contains(s, `"history"`) {
		t.Fatalf("空 history 应被省略: %s", s)
	}
	if contains(s, `"attach_log"`) {
		t.Fatalf("空 attach_log 应被省略: %s", s)
	}
	if !contains(s, `"owner_id":"demo-user"`) {
		t.Fatalf("owner_id 必填且应存在: %s", s)
	}
}

func TestHTTPErrorShape(t *testing.T) {
	e := &HTTPError{Status: 404, Code: "NOT_FOUND", Msg: "会话不存在"}
	if e.Error() != "NOT_FOUND" {
		t.Fatalf("Error() 应返回 Code，实际 %s", e.Error())
	}
}

func contains(s, sub string) bool {
	return len(s) >= len(sub) && (func() bool {
		for i := 0; i+len(sub) <= len(s); i++ {
			if s[i:i+len(sub)] == sub {
				return true
			}
		}
		return false
	})()
}