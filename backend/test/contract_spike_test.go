// S1 SPIKE：Go → Python 最小契约验证。
// 只在 SPIKE_PY_URL 设置时运行，避免日常 go test 依赖外部进程。
package test

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"
)

func baseURL(t *testing.T) string {
	u := os.Getenv("SPIKE_PY_URL")
	if u == "" {
		t.Skip("SPIKE_PY_URL 未设置，跳过契约验证")
	}
	return strings.TrimRight(u, "/")
}

func token() string { return os.Getenv("SPIKE_PY_TOKEN") }

func do(t *testing.T, client *http.Client, method, url string, body any, headers map[string]string) (int, []byte, error) {
	t.Helper()
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, url, rdr)
	if err != nil {
		t.Fatalf("构造请求失败: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := client.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, data, nil
}

// C-01 中文往返：UTF-8 不被破坏，request_id 透传。
func TestEchoChineseRoundTrip(t *testing.T) {
	base := baseURL(t)
	code, data, err := do(t, http.DefaultClient, http.MethodPost, base+"/internal/v1/echo",
		map[string]any{"text": "网络不通如何排查", "n": 1},
		map[string]string{"X-Internal-Token": token(), "X-Request-Id": "req-from-go"})
	if err != nil {
		t.Fatalf("请求失败: %v", err)
	}
	if code != 200 {
		t.Fatalf("期望 200，实际 %d: %s", code, data)
	}
	var got struct {
		RequestID string `json:"request_id"`
		EchoText  string `json:"echo_text"`
		TextLen   int    `json:"text_len"`
	}
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("解析失败: %v, body=%s", err, data)
	}
	if got.EchoText != "网络不通如何排查" {
		t.Fatalf("中文往返损坏: %q", got.EchoText)
	}
	if got.TextLen != 8 {
		t.Fatalf("字符数期望 8，实际 %d（可能被按字节计数）", got.TextLen)
	}
	if got.RequestID != "req-from-go" {
		t.Fatalf("request_id 未透传: %q", got.RequestID)
	}
}

// C-02 错误码与错误体透传：Python 的 422 + 结构体应原样到达 Go。
func TestErrorPassthrough(t *testing.T) {
	base := baseURL(t)
	code, data, err := do(t, http.DefaultClient, http.MethodGet, base+"/internal/v1/boom", nil,
		map[string]string{"X-Internal-Token": token()})
	if err != nil {
		t.Fatalf("请求失败: %v", err)
	}
	if code != 422 {
		t.Fatalf("期望 422，实际 %d: %s", code, data)
	}
	var got struct {
		Error struct {
			Code      string `json:"code"`
			RequestID string `json:"request_id"`
		} `json:"error"`
	}
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatalf("错误体结构不符合契约: %v, body=%s", err, data)
	}
	if got.Error.Code != "INVALID_INGEST_REQUEST" {
		t.Fatalf("错误码丢失: %q", got.Error.Code)
	}
}

// C-03 超时透传：客户端 2s 超时应先于服务端 3s 返回触发，且错误可区分。
func TestClientTimeout(t *testing.T) {
	base := baseURL(t)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, base+"/internal/v1/slow?ms=3000", nil)
	req.Header.Set("X-Internal-Token", token())

	start := time.Now()
	resp, err := http.DefaultClient.Do(req)
	elapsed := time.Since(start)
	if resp != nil {
		resp.Body.Close()
	}
	if err == nil {
		t.Fatalf("期望超时错误，实际成功，耗时 %v", elapsed)
	}
	if elapsed < 1500*time.Millisecond {
		t.Fatalf("耗时 %v 过短，说明连接直接失败而非读超时；服务未就绪时本用例无意义", elapsed)
	}
	if elapsed > 2500*time.Millisecond {
		t.Fatalf("超时未按预期触发：耗时 %v", elapsed)
	}
	// Go 侧应能把它识别为超时（不是服务端 4xx/5xx）
	var netErr interface{ Timeout() bool }
	if !(asTimeout(err, &netErr) && netErr.Timeout()) {
		t.Logf("错误类型为 %T，未被识别为 Timeout（需在客户端封装中按 context.DeadlineExceeded 判断）", err)
	}
}

func asTimeout(err error, target *interface{ Timeout() bool }) bool {
	type timeoutErr interface{ Timeout() bool }
	if te, ok := err.(timeoutErr); ok {
		*target = te
		return true
	}
	return false
}

// C-04 内部令牌三态 + health 豁免。
func TestInternalTokenStates(t *testing.T) {
	base := baseURL(t)
	path := base + "/internal/v1/echo"
	body := map[string]any{"text": "x"}

	if code, _, err := do(t, http.DefaultClient, http.MethodPost, path, body, nil); err != nil || code != 401 {
		t.Fatalf("缺失令牌应 401，实际 %d err=%v", code, err)
	}
	if code, _, err := do(t, http.DefaultClient, http.MethodPost, path, body,
		map[string]string{"X-Internal-Token": "wrong-token"}); err != nil || code != 401 {
		t.Fatalf("错误令牌应 401，实际 %d err=%v", code, err)
	}
	if code, data, err := do(t, http.DefaultClient, http.MethodPost, path, body,
		map[string]string{"X-Internal-Token": token()}); err != nil || code != 200 {
		t.Fatalf("正确令牌应 200，实际 %d body=%s err=%v", code, data, err)
	}

	for _, p := range []string{"/health/live", "/health/deps"} {
		if code, _, err := do(t, http.DefaultClient, http.MethodGet, base+p, nil, nil); err != nil || code != 200 {
			t.Fatalf("%s 应免鉴权 200，实际 %d err=%v", p, code, err)
		}
	}
}

// C-05 Python 不可达：Go 侧应得到可区分的连接错误（503 映射属 S3 业务层）。
func TestPythonDown(t *testing.T) {
	baseURL(t) // 仅在 SPIKE 模式下运行
	_, _, err := do(t, &http.Client{Timeout: 2 * time.Second}, http.MethodGet,
		"http://127.0.0.1:18099/health/live", nil, nil)
	if err == nil {
		t.Fatal("期望连接失败，实际成功")
	}
	t.Logf("连接不可达时错误可识别: %T", err)
}
