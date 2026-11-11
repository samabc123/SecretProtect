package main

import (
	"encoding/json"
	"testing"
	"time"
)

func TestExtractArchiveQuestionResponsesPayload(t *testing.T) {
	payload := json.RawMessage(`{"input":[{"role":"user","content":[{"type":"input_text","text":"请用 Python 分析实验数据"},{"type":"input_image","image_url":"[image omitted by archive policy]"}]}]}`)
	got := extractArchiveQuestion(payload)
	if got != "请用 Python 分析实验数据" {
		t.Fatalf("unexpected question: %q", got)
	}
}

func TestExtractArchiveQuestionChatPayload(t *testing.T) {
	payload := json.RawMessage(`{"messages":[{"role":"user","content":"如何设计消融实验？"}]}`)
	if got := extractArchiveQuestion(payload); got != "如何设计消融实验？" {
		t.Fatalf("unexpected question: %q", got)
	}
}

func TestArchiveKeywordExclusion(t *testing.T) {
	cases := []string{
		"Another language model started to solve this problem and produced a summary",
		"<environment_context><cwd>/tmp</cwd></environment_context>",
		"You are a helpful assistant. You will be presented with a user prompt",
	}
	for _, value := range cases {
		if got := archiveKeywordExclusion(value); got != "internal_request" {
			t.Fatalf("expected internal_request for %q, got %q", value, got)
		}
	}
}

func TestExtractArchiveKeywords(t *testing.T) {
	items := extractArchiveKeywords("用 Python 和 PostgreSQL 构建 RAG 知识库并进行模型训练，代码报错")
	want := map[string]bool{"Python": false, "PostgreSQL": false, "RAG": false, "知识库": false, "模型训练": false, "代码报错": false}
	for _, item := range items {
		if _, ok := want[item.Keyword]; ok {
			want[item.Keyword] = true
		}
	}
	for keyword, found := range want {
		if !found {
			t.Errorf("keyword %q was not extracted: %#v", keyword, items)
		}
	}
	if len(items) > 18 {
		t.Fatalf("too many keywords: %d", len(items))
	}
}

func TestASCIIKeywordUsesTokenBoundary(t *testing.T) {
	items := extractArchiveKeywords("This paragraph discusses reactance in a scientific instrument.")
	for _, item := range items {
		if item.Keyword == "React" {
			t.Fatal("React must not match the word reactance")
		}
	}
}

func TestNextArchiveKeywordSnapshotDelay(t *testing.T) {
	shanghai := time.FixedZone("Asia/Shanghai", 8*60*60)
	tests := []struct {
		name        string
		now         time.Time
		generatedAt time.Time
		want        time.Duration
	}{
		{
			name: "missing snapshot refreshes shortly",
			now:  time.Date(2026, 10, 8, 10, 0, 0, 0, shanghai),
			want: keywordSnapshotFirstDelay,
		},
		{
			name:        "stale snapshot refreshes shortly",
			now:         time.Date(2026, 10, 8, 10, 0, 0, 0, shanghai),
			generatedAt: time.Date(2026, 10, 7, 3, 0, 0, 0, shanghai),
			want:        keywordSnapshotFirstDelay,
		},
		{
			name:        "fresh snapshot waits until next three oclock",
			now:         time.Date(2026, 10, 8, 10, 0, 0, 0, shanghai),
			generatedAt: time.Date(2026, 10, 8, 3, 5, 0, 0, shanghai),
			want:        17 * time.Hour,
		},
		{
			name:        "before schedule waits for same day",
			now:         time.Date(2026, 10, 8, 2, 0, 0, 0, shanghai),
			generatedAt: time.Date(2026, 10, 7, 3, 5, 0, 0, shanghai),
			want:        time.Hour,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := nextArchiveKeywordSnapshotDelay(test.now, test.generatedAt); got != test.want {
				t.Fatalf("delay = %s, want %s", got, test.want)
			}
		})
	}
}
