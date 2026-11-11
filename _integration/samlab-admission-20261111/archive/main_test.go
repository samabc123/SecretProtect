package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"sync"
	"testing"
	"time"
)

type memoryArchiveStore struct {
	mu                sync.Mutex
	inserts           []archiveInsert
	pending           []completedArchive
	page              archivePage
	detail            *conversationArchive
	trajectoryPage    trajectoryPage
	trajectoryDetail  *agentTrajectory
	trajectoryEvents  trajectoryEventPage
	trajectoryImports []trajectoryImportRecord
	trajectoryStats   trajectoryImportStats
	trajectoryError   string
}

func (s *memoryArchiveStore) QueueCompleted(_ context.Context, item completedArchive) (archiveQueueResult, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.pending = append(s.pending, item)
	s.inserts = append(s.inserts, archiveInsert{
		UserID: 11, APIKeyID: 22, ClientRequestID: item.ClientRequestID,
		RequestID: item.RequestID, Protocol: item.Protocol, Endpoint: item.Endpoint,
		Model: item.Model, RequestPayload: item.RequestPayload, AssistantText: item.AssistantText,
		ToolCalls: item.ToolCalls, FinalResponse: item.FinalResponse,
		StartedAt: item.StartedAt, CompletedAt: item.CompletedAt, DurationMS: item.DurationMS,
		ResponseStatus: item.ResponseStatus, PrivacyEntities: item.PrivacyEntities, ExpiresAt: item.ExpiresAt,
	})
	return archiveQueueResult{Queued: true, Inserted: true}, nil
}

func (s *memoryArchiveStore) ResolveDue(context.Context, time.Time, int) (archiveResolveStats, error) {
	return archiveResolveStats{}, nil
}
func (s *memoryArchiveStore) DeleteExpired(context.Context, time.Time, int) (int64, error) {
	return 0, nil
}
func (s *memoryArchiveStore) ListArchives(context.Context, archiveQuery) (archivePage, error) {
	return s.page, nil
}
func (s *memoryArchiveStore) GetArchive(context.Context, int64) (*conversationArchive, error) {
	if s.detail == nil {
		return nil, sql.ErrNoRows
	}
	return s.detail, nil
}
func (s *memoryArchiveStore) ResolveArchiveUserByEmail(context.Context, string) (archiveUser, error) {
	return archiveUser{UserID: 11, Email: "tester@example.com", Role: "user"}, nil
}
func (s *memoryArchiveStore) StartTrajectoryBatch(_ context.Context, item trajectoryBatchStart) (trajectoryBatchState, error) {
	return trajectoryBatchState{ID: item.ID}, nil
}

func (s *memoryArchiveStore) ImportTrajectory(_ context.Context, _ string, _ int64, record trajectoryImportRecord) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.trajectoryImports = append(s.trajectoryImports, record)
	return "inserted", nil
}
func (s *memoryArchiveStore) FinishTrajectoryBatch(_ context.Context, _ string, stats trajectoryImportStats, errorSummary string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.trajectoryStats = stats
	s.trajectoryError = errorSummary
	return nil
}
func (s *memoryArchiveStore) ListTrajectories(context.Context, trajectoryQuery) (trajectoryPage, error) {
	return s.trajectoryPage, nil
}
func (s *memoryArchiveStore) GetTrajectory(context.Context, int64) (*agentTrajectory, error) {
	if s.trajectoryDetail == nil {
		return nil, sql.ErrNoRows
	}
	return s.trajectoryDetail, nil
}
func (s *memoryArchiveStore) ListTrajectoryEvents(context.Context, int64, int, int) (trajectoryEventPage, error) {
	return s.trajectoryEvents, nil
}
func (s *memoryArchiveStore) CheckCompatibility(context.Context) error { return nil }
func (s *memoryArchiveStore) Ping(context.Context) error               { return nil }
func (s *memoryArchiveStore) Close() error                             { return nil }
func (s *memoryArchiveStore) items() []archiveInsert {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]archiveInsert(nil), s.inserts...)
}

func newPrivacyTestServer(t *testing.T, redact func(string) (string, bool, int)) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.Method == http.MethodGet && req.URL.Path == "/health" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"status":"ok"}`)
			return
		}
		if req.Method != http.MethodPost || req.URL.Path != "/redact/batch" {
			http.NotFound(w, req)
			return
		}
		var input struct {
			Texts []string `json:"texts"`
		}
		if err := json.NewDecoder(req.Body).Decode(&input); err != nil {
			t.Errorf("decode privacy request: %v", err)
			return
		}
		results := make([]privacyBatchResponse, len(input.Texts))
		for index, text := range input.Texts {
			redacted, hit, count := redact(text)
			results[index] = privacyBatchResponse{Redacted: redacted, Hit: hit, Count: count}
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(results)
	}))
}

func proxyTestConfig(upstream, privacy *httptest.Server) config {
	upstreamURL, _ := url.Parse(upstream.URL)
	privacyURL, _ := url.Parse(privacy.URL)
	return config{
		UpstreamURL: upstreamURL, PrivacyFilterURL: privacyURL,
		PrivacyTimeout: 2 * time.Second, PrivacyMaxBody: 1 << 20,
		Retention: 30 * 24 * time.Hour, DatabaseTimeout: 2 * time.Second,
		IdentityResolveTimeout: 30 * time.Second, ResolveInterval: time.Second,
		ResolveBatchSize: 100, AdminAuthTimeout: 2 * time.Second,
	}
}

func TestBooleanEnv(t *testing.T) {
	t.Run("fallback", func(t *testing.T) {
		t.Setenv("TEST_BOOLEAN_ENV", "")
		value, err := booleanEnv("TEST_BOOLEAN_ENV", true)
		if err != nil || !value {
			t.Fatalf("booleanEnv fallback = %v, %v; want true, nil", value, err)
		}
	})
	t.Run("disabled", func(t *testing.T) {
		t.Setenv("TEST_BOOLEAN_ENV", "false")
		value, err := booleanEnv("TEST_BOOLEAN_ENV", true)
		if err != nil || value {
			t.Fatalf("booleanEnv false = %v, %v; want false, nil", value, err)
		}
	})
	t.Run("invalid", func(t *testing.T) {
		t.Setenv("TEST_BOOLEAN_ENV", "disabled")
		if _, err := booleanEnv("TEST_BOOLEAN_ENV", true); err == nil {
			t.Fatal("booleanEnv accepted an invalid value")
		}
	})
}

func TestProtocolAccumulatorsHandleFragmentedSSE(t *testing.T) {
	tests := []struct {
		name         string
		protocol     streamProtocol
		stream       string
		wantText     string
		wantToolName string
	}{
		{
			name: "openai chat", protocol: protocolOpenAIChat,
			stream: "data: {\"choices\":[{\"delta\":{\"content\":\"hel\",\"tool_calls\":[{\"index\":0,\"id\":\"call_1\",\"type\":\"function\",\"function\":{\"name\":\"lookup\",\"arguments\":\"{\\\"q\\\":\"}}]}}]}\n\n" +
				"data: {\"choices\":[{\"delta\":{\"content\":\"lo\",\"tool_calls\":[{\"index\":0,\"function\":{\"arguments\":\"\\\"x\\\"}\"}}]},\"finish_reason\":\"tool_calls\"}]}\n\n" +
				"data: [DONE]\n\n",
			wantText: "hello", wantToolName: "lookup",
		},
		{
			name: "openai responses", protocol: protocolOpenAIResponses,
			stream: "event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"hello\"}\n\n" +
				"event: response.function_call_arguments.delta\ndata: {\"type\":\"response.function_call_arguments.delta\",\"item_id\":\"fc_1\",\"delta\":\"{\\\"x\\\":1}\"}\n\n" +
				"event: response.output_item.added\ndata: {\"type\":\"response.output_item.added\",\"item\":{\"type\":\"function_call\",\"id\":\"fc_1\",\"call_id\":\"call_1\",\"name\":\"lookup\"}}\n\n" +
				"event: response.completed\ndata: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\"}}\n\n",
			wantText: "hello", wantToolName: "lookup",
		},
		{
			name: "anthropic", protocol: protocolAnthropic,
			stream: "event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"hel\"}}\n\n" +
				"event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"lo\"}}\n\n" +
				"event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":1,\"content_block\":{\"type\":\"tool_use\",\"id\":\"tool_1\",\"name\":\"lookup\",\"input\":{}}}\n\n" +
				"event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":1,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{\\\"x\\\":1}\"}}\n\n" +
				"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n",
			wantText: "hello", wantToolName: "lookup",
		},
		{
			name: "gemini", protocol: protocolGemini,
			stream: "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"hel\"}]}}]}\n\n" +
				"data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"lo\"},{\"functionCall\":{\"name\":\"lookup\",\"args\":{\"x\":1}}}]},\"finishReason\":\"STOP\"}]}\n\n",
			wantText: "hello", wantToolName: "lookup",
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			accumulator := newConversationAccumulator(test.protocol)
			parser := newSSEParser(accumulator.consume)
			for index := 0; index < len(test.stream); index += 3 {
				end := index + 3
				if end > len(test.stream) {
					end = len(test.stream)
				}
				parser.Write([]byte(test.stream[index:end]))
			}
			parser.Finish()
			if !accumulator.complete {
				t.Fatal("completion marker was not recognized")
			}
			if got := accumulator.text.String(); got != test.wantText {
				t.Fatalf("assistant text=%q, want %q", got, test.wantText)
			}
			toolsJSON, finalJSON, err := accumulator.result("test-model")
			if err != nil {
				t.Fatal(err)
			}
			if test.wantToolName != "" && !strings.Contains(string(toolsJSON), test.wantToolName) {
				t.Fatalf("tool calls=%s", toolsJSON)
			}
			if strings.Contains(string(finalJSON), "delta") || strings.Contains(string(finalJSON), "data:") {
				t.Fatalf("final response contains raw stream data: %s", finalJSON)
			}
		})
	}
}

func TestProxyStoresOneCompletedConversationAndPreservesBytes(t *testing.T) {
	const secret = "PRIVATE_TOKEN_12345"
	privacy := newPrivacyTestServer(t, func(text string) (string, bool, int) {
		redacted := strings.ReplaceAll(text, secret, "[REDACTED]")
		if redacted != text {
			return redacted, true, 1
		}
		return redacted, false, 0
	})
	defer privacy.Close()

	stream := "data: {\"choices\":[{\"delta\":{\"content\":\"assistant PRIVATE_TOKEN_12345\",\"tool_calls\":[{\"index\":0,\"id\":\"call_1\",\"type\":\"function\",\"function\":{\"name\":\"lookup\",\"arguments\":\"{\\\"token\\\":\\\"PRIVATE_TOKEN_12345\\\"}\"}}]},\"finish_reason\":\"tool_calls\"}]}\n\n" +
		"data: [DONE]\n\n"
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		body, _ := io.ReadAll(req.Body)
		if !strings.Contains(string(body), secret) || strings.Contains(string(body), "[REDACTED]") {
			t.Errorf("upstream did not receive the original body: %s", body)
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set(clientRequestIDHeader, "9f7307de-ea68-41ac-bdea-059098ae1a82")
		_, _ = io.WriteString(w, stream)
	}))
	defer upstream.Close()

	store := &memoryArchiveStore{}
	proxy := httptest.NewServer(newProxyServer(proxyTestConfig(upstream, privacy), store))
	defer proxy.Close()
	resp, err := http.Post(proxy.URL+"/v1/chat/completions", "application/json",
		strings.NewReader(`{"model":"gpt-test","stream":true,"messages":[{"role":"user","content":"PRIVATE_TOKEN_12345"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if string(body) != stream {
		t.Fatalf("stream bytes changed:\n%q\nwant:\n%q", body, stream)
	}

	deadline := time.Now().Add(time.Second)
	for len(store.items()) != 1 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	items := store.items()
	if len(items) != 1 {
		t.Fatalf("archive rows=%d, want 1", len(items))
	}
	item := items[0]
	for name, value := range map[string]string{
		"request": string(item.RequestPayload), "assistant": item.AssistantText,
		"tool_calls": string(item.ToolCalls), "final_response": string(item.FinalResponse),
	} {
		if strings.Contains(value, secret) || !strings.Contains(value, "[REDACTED]") {
			t.Fatalf("stored %s was not redacted: %s", name, value)
		}
	}
	if item.PrivacyEntities < 4 {
		t.Fatalf("privacy entities=%d, want at least 4", item.PrivacyEntities)
	}
	if strings.Contains(string(item.FinalResponse), "data:") || strings.Contains(string(item.FinalResponse), `"delta"`) {
		t.Fatalf("raw SSE was stored: %s", item.FinalResponse)
	}
}

func TestProxyDiscardsUnsuccessfulIncompleteAndNonStreamingResponses(t *testing.T) {
	privacy := newPrivacyTestServer(t, func(text string) (string, bool, int) {
		return text, false, 0
	})
	defer privacy.Close()

	tests := []struct {
		name        string
		status      int
		contentType string
		body        string
		requestBody string
	}{
		{"upstream error", http.StatusBadGateway, "text/event-stream", "data: [DONE]\n\n", `{"stream":true,"messages":[]}`},
		{"no completion marker", http.StatusOK, "text/event-stream", `data: {"choices":[{"delta":{"content":"partial"}}]}` + "\n\n", `{"stream":true,"messages":[]}`},
		{"non streaming response", http.StatusOK, "application/json", `{"choices":[]}`, `{"stream":false,"messages":[]}`},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.Header().Set("Content-Type", test.contentType)
				w.Header().Set(clientRequestIDHeader, "9f7307de-ea68-41ac-bdea-059098ae1a82")
				w.WriteHeader(test.status)
				_, _ = io.WriteString(w, test.body)
			}))
			defer upstream.Close()
			store := &memoryArchiveStore{}
			proxy := httptest.NewServer(newProxyServer(proxyTestConfig(upstream, privacy), store))
			defer proxy.Close()
			resp, err := http.Post(proxy.URL+"/v1/chat/completions", "application/json", strings.NewReader(test.requestBody))
			if err != nil {
				t.Fatal(err)
			}
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			if got := len(store.items()); got != 0 {
				t.Fatalf("archive rows=%d, want 0", got)
			}
		})
	}
}

func TestArchiveBodyCloseBeforeEOFDoesNotInsert(t *testing.T) {
	store := &memoryArchiveStore{}
	server := &proxyServer{
		cfg:   config{Retention: 30 * 24 * time.Hour, DatabaseTimeout: time.Second},
		store: store,
	}
	record := &pendingArchive{
		server: server, clientRequestID: "9f7307de-ea68-41ac-bdea-059098ae1a82", requestID: "cancelled",
		protocol: protocolOpenAIChat, requestIsStream: true, eligible: true,
		accumulator: newConversationAccumulator(protocolOpenAIChat),
	}
	record.parser = newSSEParser(record.accumulator.consume)
	body := &archiveResponseBody{
		ReadCloser: io.NopCloser(strings.NewReader("data: [DONE]\n\n")),
		record:     record,
	}
	buffer := make([]byte, 5)
	_, _ = body.Read(buffer)
	_ = body.Close()
	if got := len(store.items()); got != 0 {
		t.Fatalf("archive rows=%d, want 0", got)
	}
}

func TestArchiveClientWriteFailureDoesNotInsert(t *testing.T) {
	store := &memoryArchiveStore{}
	server := &proxyServer{
		cfg:   config{Retention: 30 * 24 * time.Hour, DatabaseTimeout: time.Second},
		store: store,
	}
	record := &pendingArchive{
		server: server, clientRequestID: "9f7307de-ea68-41ac-bdea-059098ae1a82",
		requestID: "client:9f7307de-ea68-41ac-bdea-059098ae1a82",
		protocol:  protocolOpenAIChat, requestIsStream: true, eligible: true,
		requestContext: context.Background(), accumulator: newConversationAccumulator(protocolOpenAIChat),
	}
	record.parser = newSSEParser(record.accumulator.consume)
	record.parser.Write([]byte("data: [DONE]\n\n"))
	record.clientWriteFailed.Store(true)
	record.finish(true, nil)
	if got := len(store.items()); got != 0 {
		t.Fatalf("archive rows=%d, want 0", got)
	}
}

func TestArchiveEmptyAssistantTextDoesNotInsert(t *testing.T) {
	tests := []struct {
		name   string
		stream string
	}{
		{
			name: "empty",
			stream: "event: response.completed\ndata: " +
				`{"type":"response.completed","response":{"status":"completed"}}` + "\n\n",
		},
		{
			name: "whitespace only",
			stream: "event: response.output_text.delta\ndata: " +
				`{"type":"response.output_text.delta","delta":" \n\t"}` + "\n\n" +
				"event: response.completed\ndata: " +
				`{"type":"response.completed","response":{"status":"completed"}}` + "\n\n",
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			store := &memoryArchiveStore{}
			server := &proxyServer{
				cfg:   config{Retention: 30 * 24 * time.Hour, DatabaseTimeout: time.Second},
				store: store,
			}
			record := &pendingArchive{
				server: server, clientRequestID: "9f7307de-ea68-41ac-bdea-059098ae1a82",
				requestID: "client:9f7307de-ea68-41ac-bdea-059098ae1a82",
				protocol:  protocolOpenAIResponses, requestIsStream: true, eligible: true,
				requestContext: context.Background(), accumulator: newConversationAccumulator(protocolOpenAIResponses),
			}
			record.parser = newSSEParser(record.accumulator.consume)
			record.parser.Write([]byte(test.stream))
			record.finish(true, nil)

			if got := len(store.items()); got != 0 {
				t.Fatalf("archive rows=%d, want 0", got)
			}
			if got := server.metrics.discardedStreams.Load(); got != 1 {
				t.Fatalf("discarded streams=%d, want 1", got)
			}
		})
	}
}

func TestPrivacyFilterRedactsConversationTextAndPreservesStructure(t *testing.T) {
	privacy := newPrivacyTestServer(t, func(text string) (string, bool, int) {
		redacted := strings.ReplaceAll(text, "alice@example.com", "[EMAIL]")
		if redacted != text {
			return redacted, true, 1
		}
		return redacted, false, 0
	})
	defer privacy.Close()
	privacyURL, _ := url.Parse(privacy.URL)
	client := newPrivacyClient(config{
		PrivacyFilterURL: privacyURL, PrivacyTimeout: 2 * time.Second, PrivacyMaxBody: 1 << 20,
	})
	body := `{"model":"gpt-test","messages":[{"role":"user","content":"email alice@example.com","tool_calls":[{"id":"call_alice@example.com","function":{"name":"lookup","arguments":"{\"email\":\"alice@example.com\"}"}}],"image_url":{"url":"https://example.test/alice@example.com.png"}}]}`
	filtered, stats, err := client.redactJSON(context.Background(), []byte(body))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(filtered), `"content":"email alice@example.com"`) {
		t.Fatalf("content not filtered: %s", filtered)
	}
	if !strings.Contains(string(filtered), "call_alice@example.com") ||
		!strings.Contains(string(filtered), "https://example.test/alice@example.com.png") {
		t.Fatalf("non-content identifiers changed: %s", filtered)
	}
	if stats.Entities != 2 {
		t.Fatalf("privacy entities=%d, want 2", stats.Entities)
	}
}

func TestPrivacyFilterSanitizesPostgresUnsupportedNulls(t *testing.T) {
	privacySawNull := false
	privacy := newPrivacyTestServer(t, func(text string) (string, bool, int) {
		if strings.ContainsRune(text, '\x00') {
			privacySawNull = true
		}
		return text + "\x00", false, 0
	})
	defer privacy.Close()
	privacyURL, _ := url.Parse(privacy.URL)
	client := newPrivacyClient(config{
		PrivacyFilterURL: privacyURL, PrivacyTimeout: 2 * time.Second, PrivacyMaxBody: 1 << 20,
	})
	body := `{"model":"gpt\u0000test","messages":[{"role":"user","content":"alpha\u0000beta"}],"metadata":{"raw":"gamma\u0000delta"},"bad\u0000key":"value"}`
	filtered, _, err := client.redactJSON(context.Background(), []byte(body))
	if err != nil {
		t.Fatal(err)
	}
	if privacySawNull {
		t.Fatal("privacy filter received a PostgreSQL-incompatible NUL")
	}
	if strings.ContainsRune(string(filtered), '\x00') || strings.Contains(string(filtered), `\u0000`) {
		t.Fatalf("filtered JSON still contains a PostgreSQL-incompatible NUL: %s", filtered)
	}
	var document map[string]any
	if err := json.Unmarshal(filtered, &document); err != nil {
		t.Fatal(err)
	}
	if document["model"] != "gpt\uFFFDtest" {
		t.Fatalf("model=%q", document["model"])
	}
	messages := document["messages"].([]any)
	content := messages[0].(map[string]any)["content"]
	if content != "alpha\uFFFDbeta\uFFFD" {
		t.Fatalf("content=%q", content)
	}
	metadata := document["metadata"].(map[string]any)
	if metadata["raw"] != "gamma\uFFFDdelta" {
		t.Fatalf("metadata.raw=%q", metadata["raw"])
	}
	if document["bad\uFFFDkey"] != "value" {
		t.Fatalf("sanitized key missing: %#v", document)
	}
}

func TestRedactArchiveSanitizesDatabaseTextColumns(t *testing.T) {
	privacy := newPrivacyTestServer(t, func(text string) (string, bool, int) { return text, false, 0 })
	defer privacy.Close()
	privacyURL, _ := url.Parse(privacy.URL)
	client := newPrivacyClient(config{
		PrivacyFilterURL: privacyURL, PrivacyTimeout: 2 * time.Second, PrivacyMaxBody: 1 << 20,
	})
	item, err := client.redactArchive(context.Background(), completedArchive{
		RequestID:      "client:test\x00request",
		Protocol:       "openai\x00chat",
		Endpoint:       "/v1/chat\x00/completions",
		Model:          "gpt\x00test",
		RequestPayload: json.RawMessage(`{"messages":[{"role":"user","content":"question"}]}`),
		AssistantText:  "answer\x00complete",
		ToolCalls:      json.RawMessage(`[]`),
		FinalResponse:  json.RawMessage(`{"assistant_text":"answer\u0000complete"}`),
	})
	if err != nil {
		t.Fatal(err)
	}
	for label, value := range map[string]string{
		"request_id": item.RequestID, "protocol": item.Protocol, "endpoint": item.Endpoint,
		"model": item.Model, "assistant_text": item.AssistantText,
	} {
		if strings.ContainsRune(value, '\x00') || !strings.ContainsRune(value, '\uFFFD') {
			t.Fatalf("%s was not sanitized: %q", label, value)
		}
	}
	if strings.Contains(string(item.FinalResponse), `\u0000`) {
		t.Fatalf("final response was not sanitized: %s", item.FinalResponse)
	}
}

func TestProxyStillCallsUpstreamButSkipsArchiveWhenPrivacyFilterFails(t *testing.T) {
	privacy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.Method == http.MethodGet && req.URL.Path == "/health" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"status":"ok"}`)
			return
		}
		http.Error(w, "privacy rules failed to load", http.StatusInternalServerError)
	}))
	defer privacy.Close()
	upstreamCalls := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		upstreamCalls++
		body, _ := io.ReadAll(req.Body)
		if !strings.Contains(string(body), `"content":"secret"`) {
			t.Errorf("upstream did not receive original request: %s", body)
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set(clientRequestIDHeader, "9f7307de-ea68-41ac-bdea-059098ae1a82")
		_, _ = io.WriteString(w, "data: {\"choices\":[{\"delta\":{\"content\":\"answer\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n")
	}))
	defer upstream.Close()
	store := &memoryArchiveStore{}
	server := newProxyServer(proxyTestConfig(upstream, privacy), store)
	proxy := httptest.NewServer(server)
	defer proxy.Close()

	response, err := http.Post(proxy.URL+"/v1/chat/completions", "application/json",
		strings.NewReader(`{"model":"gpt-test","stream":true,"messages":[{"role":"user","content":"secret"}]}`))
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(response.Body)
	_ = response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status=%d body=%s", response.StatusCode, body)
	}
	if !strings.Contains(string(body), "answer") || !strings.Contains(string(body), "[DONE]") {
		t.Fatalf("upstream stream was not delivered: %q", body)
	}
	deadline := time.Now().Add(time.Second)
	for server.metrics.privacyFailures.Load() != 1 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if upstreamCalls != 1 || len(store.items()) != 0 || server.metrics.privacyFailures.Load() != 1 {
		t.Fatalf("upstream_calls=%d archives=%d privacy_failures=%d",
			upstreamCalls, len(store.items()), server.metrics.privacyFailures.Load())
	}

	healthResponse, err := http.Get(proxy.URL + "/_archive/health")
	if err != nil {
		t.Fatal(err)
	}
	healthBody, _ := io.ReadAll(healthResponse.Body)
	_ = healthResponse.Body.Close()
	if healthResponse.StatusCode != http.StatusOK || !strings.Contains(string(healthBody), `"privacy_filter":"degraded"`) {
		t.Fatalf("health status=%d body=%s, want degraded 200", healthResponse.StatusCode, healthBody)
	}
}
func TestShouldArchiveConversationRoutesOnly(t *testing.T) {
	tests := []struct {
		method, path, upgrade string
		want                  bool
	}{
		{http.MethodPost, "/v1/messages", "", true},
		{http.MethodPost, "/v1/chat/completions", "", true},
		{http.MethodPost, "/responses", "", true},
		{http.MethodPost, "/v1beta/models/gemini:streamGenerateContent", "", true},
		{http.MethodPost, "/v1beta/models/gemini:generateContent", "", false},
		{http.MethodPost, "/v1/alpha/search", "", false},
		{http.MethodGet, "/v1/responses", "websocket", true},
		{http.MethodPost, "/v1/messages/count_tokens", "", false},
		{http.MethodPost, "/api/v1/auth/login", "", false},
	}
	for _, test := range tests {
		req := httptest.NewRequest(test.method, test.path, nil)
		req.Header.Set("Upgrade", test.upgrade)
		if got := shouldArchive(req); got != test.want {
			t.Errorf("shouldArchive(%s %s)=%v, want %v", test.method, test.path, got, test.want)
		}
	}
}

func TestRequestMetadataExtractsGeminiModelFromPath(t *testing.T) {
	model, stream := requestMetadata(
		[]byte(`{"contents":[{"role":"user","parts":[{"text":"hello"}]}]}`),
		"/v1beta/models/gemini-2.5-pro:streamGenerateContent",
		protocolGemini,
	)
	if model != "gemini-2.5-pro" || !stream {
		t.Fatalf("model=%q stream=%v", model, stream)
	}
}

func TestProxyRejectsConversationWebSocket(t *testing.T) {
	privacy := newPrivacyTestServer(t, func(text string) (string, bool, int) { return text, false, 0 })
	defer privacy.Close()
	upstreamCalls := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { upstreamCalls++ }))
	defer upstream.Close()
	store := &memoryArchiveStore{}
	proxy := httptest.NewServer(newProxyServer(proxyTestConfig(upstream, privacy), store))
	defer proxy.Close()
	req, _ := http.NewRequest(http.MethodGet, proxy.URL+"/v1/responses", nil)
	req.Header.Set("Upgrade", "websocket")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusNotImplemented || upstreamCalls != 0 {
		t.Fatalf("status=%d upstream_calls=%d", resp.StatusCode, upstreamCalls)
	}
}

func TestProxyStripsSpoofedIdentityHeadersOnNonArchiveRoutes(t *testing.T) {
	privacy := newPrivacyTestServer(t, func(text string) (string, bool, int) { return text, false, 0 })
	defer privacy.Close()
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.Header.Get(archiveUserIDHeader) != "" ||
			req.Header.Get(archiveAPIKeyIDHeader) != "" ||
			req.Header.Get(clientRequestIDHeader) != "" {
			t.Error("spoofed identity header reached upstream")
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer upstream.Close()
	proxy := httptest.NewServer(newProxyServer(proxyTestConfig(upstream, privacy), &memoryArchiveStore{}))
	defer proxy.Close()
	req, _ := http.NewRequest(http.MethodGet, proxy.URL+"/api/v1/auth/me", nil)
	req.Header.Set(archiveUserIDHeader, "999")
	req.Header.Set(archiveAPIKeyIDHeader, "888")
	req.Header.Set(clientRequestIDHeader, "00000000-0000-0000-0000-000000000000")
	response, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if response.StatusCode != http.StatusNoContent {
		t.Fatalf("status=%d", response.StatusCode)
	}
}

func TestProxyDiscardsSuccessfulStreamWithoutClientRequestID(t *testing.T) {
	privacy := newPrivacyTestServer(t, func(text string) (string, bool, int) { return text, false, 0 })
	defer privacy.Close()
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: [DONE]\n\n")
	}))
	defer upstream.Close()
	store := &memoryArchiveStore{}
	server := newProxyServer(proxyTestConfig(upstream, privacy), store)
	proxy := httptest.NewServer(server)
	defer proxy.Close()
	resp, err := http.Post(proxy.URL+"/v1/chat/completions", "application/json",
		strings.NewReader(`{"model":"gpt-test","stream":true,"messages":[]}`))
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	if got := len(store.items()); got != 0 {
		t.Fatalf("archive rows=%d, want 0", got)
	}
	if server.metrics.missingClientIDs.Load() != 1 {
		t.Fatalf("missing client id metric=%d, want 1", server.metrics.missingClientIDs.Load())
	}
}

func TestArchiveAdminAPIReusesSub2APILogin(t *testing.T) {
	privacy := newPrivacyTestServer(t, func(text string) (string, bool, int) { return text, false, 0 })
	defer privacy.Close()
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.URL.Path != "/api/v1/auth/me" {
			http.NotFound(w, req)
			return
		}
		if req.Header.Get("Authorization") != "Bearer admin-token" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"code":0,"message":"success","data":{"id":1,"username":"admin","email":"admin@example.test","role":"admin","status":"active"}}`)
	}))
	defer upstream.Close()
	store := &memoryArchiveStore{
		page:           archivePage{Items: []*conversationArchive{{ID: 7, UserID: 11, RequestID: "client:test"}}, Total: 1, Page: 1, PageSize: 50, Pages: 1},
		trajectoryPage: trajectoryPage{Items: []*agentTrajectory{{ID: 8, UserID: 11, TrajectoryID: "codex:thread:turn", Status: "completed"}}, Total: 1, Page: 1, PageSize: 50, Pages: 1},
	}
	proxy := httptest.NewServer(newProxyServer(proxyTestConfig(upstream, privacy), store))
	defer proxy.Close()
	req, _ := http.NewRequest(http.MethodGet, proxy.URL+"/_archive/api/v1/conversation-archives", nil)
	req.Header.Set("Authorization", "Bearer admin-token")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		t.Fatalf("status=%d body=%s", resp.StatusCode, body)
	}
	var envelope apiEnvelope
	if err := json.NewDecoder(resp.Body).Decode(&envelope); err != nil {
		t.Fatal(err)
	}
	if envelope.Code != 0 {
		t.Fatalf("unexpected API response: %+v", envelope)
	}

	req, _ = http.NewRequest(http.MethodGet, proxy.URL+"/_archive/api/v1/agent-trajectories", nil)
	req.Header.Set("Authorization", "Bearer admin-token")
	resp, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		t.Fatalf("trajectory status=%d body=%s", resp.StatusCode, body)
	}
}

func TestExternalPluginMigrationContract(t *testing.T) {
	content, err := os.ReadFile("migrations/001_external_archive_plugin.sql")
	if err != nil {
		t.Fatal(err)
	}
	sqlText := string(content)
	for _, required := range []string{
		"conversation_archive_pending",
		"resolve_archive_identity",
		"SECURITY DEFINER",
		"archive_identity_ambiguous",
		"REVOKE ALL ON FUNCTION",
	} {
		if !strings.Contains(sqlText, required) {
			t.Fatalf("migration missing %q", required)
		}
	}
}

func TestAgentTrajectoryMigrationAndDeploymentContract(t *testing.T) {
	content, err := os.ReadFile("migrations/002_agent_trajectories.sql")
	if err != nil {
		t.Fatal(err)
	}
	sqlText := string(content)
	for _, required := range []string{
		"agent_trajectory_import_batches",
		"agent_trajectories",
		"agent_trajectory_events",
		"agent_trajectory_labels",
		"email_snapshot",
		"resolve_archive_user_by_email",
		"LOWER(BTRIM(u.email)) = normalized",
		"DROP FUNCTION IF EXISTS resolve_archive_user_by_username(TEXT)",
		"SECURITY DEFINER",
		"ON DELETE CASCADE",
		"REFERENCES agent_trajectory_import_batches(id) ON DELETE CASCADE",
		"DROP CONSTRAINT IF EXISTS agent_trajectories_import_batch_id_fkey",
	} {
		if !strings.Contains(sqlText, required) {
			t.Fatalf("trajectory migration missing %q", required)
		}
	}
	deploy, err := os.ReadFile("deploy/deploy_external_plugin.sh")
	if err != nil {
		t.Fatal(err)
	}
	deployText := string(deploy)
	for _, required := range []string{
		"CREATE ROLE trajectory_importer",
		"TRAJECTORY_IMPORT_DATABASE_URL",
		"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM trajectory_importer",
		"GRANT EXECUTE ON FUNCTION resolve_archive_user_by_email(TEXT)",
		"GRANT SELECT (id, email, role, status, deleted_at)",
		"SET ROLE conversation_archiver",
		"SET ROLE trajectory_importer",
		"FROM resolve_archive_user_by_email('__archive_deploy_missing_user__@invalid.example')",
	} {
		if !strings.Contains(deployText, required) {
			t.Fatalf("trajectory deployment missing %q", required)
		}
	}
	importHelper, err := os.ReadFile("deploy/import-trajectories.sh")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(importHelper), "chown 65532:65532 \"$resolved\"") {
		t.Fatal("trajectory importer does not hand the package to the non-root container user")
	}
	if !strings.Contains(string(importHelper), "-v \"$resolved:/import.ndjson.gz:ro\"") {
		t.Fatal("trajectory importer must mount only the selected package")
	}
	if !strings.Contains(string(importHelper), "import-trajectories --email \"$email\"") {
		t.Fatal("trajectory importer must bind imports by email")
	}
}

func TestAdminConversationDetailRendersStructuredMessages(t *testing.T) {
	content, err := os.ReadFile("static/admin.html")
	if err != nil {
		t.Fatal(err)
	}
	html := string(content)
	for _, required := range []string{
		"function requestMessages(payload)",
		"function structuredMessages(title,items)",
		"async function detail(id)",
		"requestMessages(x.request_payload)",
		"structuredMessages('用户最后一条消息',users)",
		"section('最终可见助手回复'",
		"collapsedJSON('精简后的归档请求'",
		"<details><summary>",
	} {
		if !strings.Contains(html, required) {
			t.Fatalf("admin detail page missing %q", required)
		}
	}
}

func TestProductionComposeUsesExistingNginxLoopbackRoute(t *testing.T) {
	content, err := os.ReadFile("deploy/docker-compose.production.yml")
	if err != nil {
		t.Fatal(err)
	}
	compose := string(content)
	if !strings.Contains(compose, "ARCHIVE_BIND_HOST") || !strings.Contains(compose, ":8081:8081") {
		t.Fatal("production archive container must bind the nginx loopback upstream on port 8081")
	}
	if strings.Contains(compose, ":80:8081") {
		t.Fatal("production archive container must not compete with nginx for host port 80")
	}

	deploy, err := os.ReadFile("deploy/deploy_external_plugin.sh")
	if err != nil {
		t.Fatal(err)
	}
	deployText := string(deploy)
	for _, required := range []string{
		"getent ahostsv4 localhost",
		"ARCHIVE_BIND_HOST=%s",
		"grep -q '^ARCHIVE_BIND_HOST='",
	} {
		if !strings.Contains(deployText, required) {
			t.Fatalf("deployment loopback contract missing %q", required)
		}
	}
}
