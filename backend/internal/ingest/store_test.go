package ingest

import (
	"strings"
	"testing"
)

func TestValidateUpload_AllowedFormats(t *testing.T) {
	cases := []struct {
		name    string
		file    string
		data    []byte
		wantErr string
	}{
		{"markdown 正常", "note.md", []byte("# 标题\n正文"), ""},
		{"txt 正常", "note.txt", []byte("纯文本内容"), ""},
		{"大写扩展名", "NOTE.MD", []byte("内容"), ""},
		{"不支持的格式", "a.pdf", []byte("x"), "UNSUPPORTED_FORMAT"},
		{"无扩展名", "noext", []byte("x"), "UNSUPPORTED_FORMAT"},
		{"空文件", "empty.md", []byte{}, "EMPTY_CONTENT"},
		{"全空白", "blank.md", []byte("   \n\n\t  "), "EMPTY_CONTENT"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, _, err := ValidateUpload(c.file, c.data)
			if c.wantErr == "" {
				if err != nil {
					t.Fatalf("期望通过，实际报错: %v", err)
				}
				return
			}
			he, ok := err.(*IngestHTTPError)
			if !ok {
				t.Fatalf("期望 IngestHTTPError，实际 %T (%v)", err, err)
			}
			if he.Code != c.wantErr {
				t.Fatalf("期望错误码 %s，实际 %s", c.wantErr, he.Code)
			}
		})
	}
}

func TestValidateUpload_TooLarge(t *testing.T) {
	big := make([]byte, MaxUploadSize+1)
	for i := range big {
		big[i] = 'a'
	}
	_, _, err := ValidateUpload("big.md", big)
	he, ok := err.(*IngestHTTPError)
	if !ok || he.Code != "DOC_TOO_LARGE" {
		t.Fatalf("期望 DOC_TOO_LARGE，实际 %v", err)
	}
}

func TestValidateUpload_TitleDerivation(t *testing.T) {
	title, text, err := ValidateUpload("k8s 排障手册.md", []byte("正文"))
	if err != nil {
		t.Fatalf("意外错误: %v", err)
	}
	if title != "k8s 排障手册" {
		t.Fatalf("标题去扩展名失败: %q", title)
	}
	if text != "正文" {
		t.Fatalf("正文不符: %q", text)
	}
}

func TestValidateUpload_UnsafeTitleFallsBack(t *testing.T) {
	// 含非法文件名字符 → 回退为 untitled
	title, _, err := ValidateUpload("a<b>c.md", []byte("正文"))
	if err != nil {
		t.Fatalf("意外错误: %v", err)
	}
	if title != "untitled" {
		t.Fatalf("非法标题未回退，实际 %q", title)
	}
}

func TestNormalizeText(t *testing.T) {
	in := "\ufeffa\r\nb\rc"
	got := NormalizeText(in)
	if strings.Contains(got, "\ufeff") {
		t.Fatalf("BOM 未去除")
	}
	if strings.Contains(got, "\r") {
		t.Fatalf("回车未归一: %q", got)
	}
	if got != "a\nb\nc" {
		t.Fatalf("换行归一不符: %q", got)
	}
}

func TestSourceHash_DeterministicAndNormalized(t *testing.T) {
	// 同一内容的 CRLF / LF 归一后应得到相同哈希
	h1 := SourceHash(NormalizeText("a\r\nb"))
	h2 := SourceHash(NormalizeText("a\nb"))
	if h1 != h2 {
		t.Fatalf("归一后哈希不一致: %s vs %s", h1, h2)
	}
	// 不同内容哈希不同
	if SourceHash("a") == SourceHash("b") {
		t.Fatalf("不同内容哈希相同")
	}
	// sha256 十六进制长度
	if len(h1) != 64 {
		t.Fatalf("哈希长度异常: %d", len(h1))
	}
}

func TestGenerateRunToken_Unique(t *testing.T) {
	seen := make(map[string]bool, 100)
	for i := 0; i < 100; i++ {
		tok, err := generateRunToken()
		if err != nil {
			t.Fatalf("生成令牌失败: %v", err)
		}
		if len(tok) != 32 {
			t.Fatalf("令牌长度应为 32 hex，实际 %d (%s)", len(tok), tok)
		}
		if seen[tok] {
			t.Fatalf("令牌重复: %s", tok)
		}
		seen[tok] = true
	}
}

func TestNewUUID_Format(t *testing.T) {
	seen := make(map[string]bool, 50)
	for i := 0; i < 50; i++ {
		id := newUUID()
		if len(id) != 36 {
			t.Fatalf("UUID 长度异常: %q", id)
		}
		// 8-4-4-4-12
		parts := strings.Split(id, "-")
		if len(parts) != 5 {
			t.Fatalf("UUID 分段异常: %q", id)
		}
		if seen[id] {
			t.Fatalf("UUID 重复: %s", id)
		}
		seen[id] = true
	}
}

func TestDecodeBytes_UTF8(t *testing.T) {
	got, err := decodeBytes([]byte("中文 with ascii"))
	if err != nil {
		t.Fatalf("UTF-8 解码失败: %v", err)
	}
	if got != "中文 with ascii" {
		t.Fatalf("解码内容不符: %q", got)
	}
}

func TestDecodeBytes_InvalidEncoding(t *testing.T) {
	// 非法 UTF-8 字节序列（0xFF 起始）应报错，V1 不支持 GBK 自动解码
	_, err := decodeBytes([]byte{0xff, 0xfe, 0xfd})
	if err == nil {
		t.Fatalf("非法编码应报错")
	}
}

func TestEstimateTokens(t *testing.T) {
	if got := estimateTokens("中文一百个字"); got <= 0 {
		t.Fatalf("token 估算应为正: %d", got)
	}
	if got := estimateTokens(""); got != 0 {
		t.Fatalf("空文本 token 应为 0，实际 %d", got)
	}
}