package main

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestCompactArchiveRequestKeepsOnlyLastUserMessage(t *testing.T) {
	tests := []struct {
		name     string
		protocol streamProtocol
		input    string
		want     string
		absent   []string
	}{
		{
			name: "openai chat", protocol: protocolOpenAIChat,
			input: `{"model":"gpt-test","stream":true,"messages":[{"role":"system","content":"system-secret"},{"role":"user","content":"old-question"},{"role":"assistant","content":"old-answer"},{"role":"user","content":[{"type":"text","text":"last-question"},{"type":"image_url","image_url":{"url":"data:image/png;base64,IMAGE-SECRET"}}]}]}`,
			want:  "last-question", absent: []string{"system-secret", "old-question", "old-answer", "IMAGE-SECRET", "data:image", "image_url"},
		},
		{
			name: "openai responses", protocol: protocolOpenAIResponses,
			input: `{"model":"gpt-test","stream":true,"instructions":"developer-secret","input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"old-question"}]},{"type":"message","role":"assistant","content":[{"type":"output_text","text":"old-answer"}]},{"type":"message","role":"user","content":[{"type":"input_text","text":"last-question"},{"type":"input_image","image_url":"https://example.test/private.png"}]}]}`,
			want:  "last-question", absent: []string{"developer-secret", "old-question", "old-answer", "https://example.test", "image_url"},
		},
		{
			name: "anthropic", protocol: protocolAnthropic,
			input: `{"model":"claude-test","stream":true,"system":"system-secret","messages":[{"role":"user","content":"old-question"},{"role":"assistant","content":"old-answer"},{"role":"user","content":[{"type":"text","text":"last-question"},{"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":"IMAGE-SECRET"}}]}]}`,
			want:  "last-question", absent: []string{"system-secret", "old-question", "old-answer", "IMAGE-SECRET", "base64", "media_type"},
		},
		{
			name: "gemini", protocol: protocolGemini,
			input: `{"systemInstruction":{"parts":[{"text":"system-secret"}]},"contents":[{"role":"user","parts":[{"text":"old-question"}]},{"role":"model","parts":[{"text":"old-answer"}]},{"role":"user","parts":[{"text":"last-question"},{"inlineData":{"mimeType":"image/png","data":"IMAGE-SECRET"}}]}]}`,
			want:  "last-question", absent: []string{"system-secret", "old-question", "old-answer", "IMAGE-SECRET", "inlineData", "mimeType"},
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			compact, err := compactArchiveRequest([]byte(test.input), test.protocol)
			if err != nil {
				t.Fatal(err)
			}
			text := string(compact)
			if !strings.Contains(text, test.want) || !strings.Contains(text, omittedImage) {
				t.Fatalf("compact payload missing last question or media marker: %s", text)
			}
			for _, forbidden := range test.absent {
				if strings.Contains(text, forbidden) {
					t.Fatalf("compact payload retained %q: %s", forbidden, text)
				}
			}
			var document map[string]any
			if err := json.Unmarshal(compact, &document); err != nil {
				t.Fatalf("compact payload is invalid JSON: %v", err)
			}
			meta := document["_archive"].(map[string]any)
			if meta["mode"] != "last_user_message" || meta["user_message_found"] != true || meta["media_omitted"].(float64) != 1 {
				t.Fatalf("unexpected archive metadata: %#v", meta)
			}
		})
	}
}

func TestCompactArchiveRequestNeverFallsBackToNonUserContext(t *testing.T) {
	compact, err := compactArchiveRequest(
		[]byte(`{"model":"gpt-test","stream":true,"messages":[{"role":"system","content":"system-secret"},{"role":"assistant","content":"assistant-secret"}]}`),
		protocolOpenAIChat,
	)
	if err != nil {
		t.Fatal(err)
	}
	text := string(compact)
	if strings.Contains(text, "system-secret") || strings.Contains(text, "assistant-secret") {
		t.Fatalf("non-user context was retained: %s", text)
	}
	if !strings.Contains(text, `"user_message_found":false`) {
		t.Fatalf("missing no-user marker: %s", text)
	}
}

func TestCompactArchiveRequestOmitsEmbeddedMediaStrings(t *testing.T) {
	compact, err := compactArchiveRequest(
		[]byte(`{"input":[{"role":"user","content":[{"type":"input_text","text":"data:image/png;base64,IMAGE-SECRET"},{"type":"input_file","file_data":"data:application/pdf;base64,FILE-SECRET"}]}]}`),
		protocolOpenAIResponses,
	)
	if err != nil {
		t.Fatal(err)
	}
	text := string(compact)
	if strings.Contains(text, "IMAGE-SECRET") || strings.Contains(text, "FILE-SECRET") ||
		!strings.Contains(text, omittedImage) || !strings.Contains(text, omittedFile) {
		t.Fatalf("embedded media was not omitted: %s", text)
	}
}
