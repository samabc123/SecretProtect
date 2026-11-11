package main

import (
	"context"
	"encoding/json"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestPolishModePromptsHaveDistinctContracts(t *testing.T) {
	expectations := map[string][]string{
		"conservative": {"minimal necessary changes", "preserve the existing structure"},
		"structured":   {"information architecture", "Markdown"},
		"concise":      {"compress", "must not add"},
		"professional": {"professional task specification", "quality criteria"},
	}
	seen := make(map[string]string, len(expectations))
	for mode, required := range expectations {
		prompt, err := buildPolishSystemPrompt(mode)
		if err != nil {
			t.Fatalf("mode %s: %v", mode, err)
		}
		if !strings.Contains(prompt, "contract version "+promptModeContractVersion) {
			t.Fatalf("mode %s missing contract version: %s", mode, prompt)
		}
		for _, phrase := range required {
			if !strings.Contains(strings.ToLower(prompt), strings.ToLower(phrase)) {
				t.Fatalf("mode %s missing %q: %s", mode, phrase, prompt)
			}
		}
		for otherMode, otherPrompt := range seen {
			if prompt == otherPrompt {
				t.Fatalf("modes %s and %s have identical prompts", mode, otherMode)
			}
		}
		seen[mode] = prompt
	}
}

func TestValidatePromptPolishRequestAssignsContextIDsAndEnforcesBudget(t *testing.T) {
	request := promptPolishRequest{
		Prompt: "Continue the deployment plan.",
		Mode:   "structured",
		Context: []promptContextMessage{
			{Role: "user", Content: "Use API v2."},
			{ID: "accepted-answer", Role: "assistant", Content: "Deploy to staging first."},
		},
	}
	if err := validatePromptPolishRequest(&request); err != nil {
		t.Fatal(err)
	}
	if request.Context[0].ID != "ctx-1" || request.Context[1].ID != "accepted-answer" {
		t.Fatalf("context IDs = %#v", request.Context)
	}

	overBudget := request
	overBudget.Context = []promptContextMessage{{Role: "user", Content: strings.Repeat("x", maxPromptContextRunes+1)}}
	if err := validatePromptPolishRequest(&overBudget); err == nil {
		t.Fatal("oversized context must fail")
	}
}

func TestDecodePolishModelResponseValidatesModeAndContextIDs(t *testing.T) {
	content := `{"optimized_prompt":"Use API v2.","changes":["Resolved the API reference."],"clarifying_questions":[],"used_memory_ids":[7],"used_context_message_ids":["ctx-1","invented"],"resolved_references":["API means API v2."],"applied_constraints":["Use API v2."],"mode_applied":"professional","mode_contract_version":"2","confidence":0.9}`
	result, err := decodePolishModelResponse(content, map[int64]struct{}{7: {}}, map[string]struct{}{"ctx-1": {}}, "professional")
	if err != nil {
		t.Fatal(err)
	}
	if result.ModeApplied != "professional" || result.ModeContractVersion != promptModeContractVersion {
		t.Fatalf("mode metadata = %#v", result)
	}
	if len(result.UsedContextMessageIDs) != 1 || result.UsedContextMessageIDs[0] != "ctx-1" || result.IgnoredContextIDs != 1 {
		t.Fatalf("context IDs = %#v", result)
	}
	if _, err := decodePolishModelResponse(strings.Replace(content, `"mode_applied":"professional"`, `"mode_applied":"concise"`, 1), map[int64]struct{}{7: {}}, map[string]struct{}{"ctx-1": {}}, "professional"); err == nil {
		t.Fatal("a response for the wrong mode must fail")
	}
}

func TestDecodePolishModelResponseNormalizesNumericContractVersion(t *testing.T) {
	content := `{"optimized_prompt":"Keep API v2.","changes":[],"clarifying_questions":[],"used_memory_ids":[],"used_context_message_ids":[],"resolved_references":[],"applied_constraints":[],"mode_applied":"conservative","mode_contract_version":2,"confidence":1}`
	result, err := decodePolishModelResponse(content, map[int64]struct{}{}, map[string]struct{}{}, "conservative")
	if err != nil {
		t.Fatal(err)
	}
	if result.ModeContractVersion != promptModeContractVersion {
		t.Fatalf("contract version = %q", result.ModeContractVersion)
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(encoded), `"mode_contract_version":"2"`) {
		t.Fatalf("normalized response must expose a string version: %s", encoded)
	}
}

func TestDecodePolishModelResponseNormalizesScalarStringLists(t *testing.T) {
	content := `{"optimized_prompt":"Keep API v2.","changes":"Clarified wording.","clarifying_questions":[],"used_memory_ids":[],"used_context_message_ids":"ctx-1","resolved_references":[],"applied_constraints":"Keep API v2.","mode_applied":"structured","mode_contract_version":"2","confidence":0.8}`
	result, err := decodePolishModelResponse(content, map[int64]struct{}{}, map[string]struct{}{"ctx-1": {}}, "structured")
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Changes) != 1 || len(result.UsedContextMessageIDs) != 1 || len(result.AppliedConstraints) != 1 {
		t.Fatalf("scalar string fields were not normalized: %#v", result)
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(encoded), `"applied_constraints":["Keep API v2."]`) {
		t.Fatalf("normalized response must expose arrays: %s", encoded)
	}
}

func TestValidateProtectedSpansAllowsUsedEvidenceLiterals(t *testing.T) {
	original := "Continue the deployment plan."
	optimized := "Deploy API v2 to /srv/sub2api on 2026-08-12."
	evidence := []string{"Use API v2 and deploy to /srv/sub2api on 2026-08-12."}
	if failures := validateProtectedSpansWithEvidence(original, optimized, evidence); len(failures) != 0 {
		t.Fatalf("evidence-backed literals must be allowed: %v", failures)
	}
	unsupported := "Deploy API v3 to /srv/sub2api on 2026-08-12."
	if failures := validateProtectedSpansWithEvidence(original, unsupported, evidence); len(failures) == 0 {
		t.Fatal("unsupported literals must fail")
	}
}

func TestExtractPromptContextMessagesPreservesRolesAndExcludesUnsafeContent(t *testing.T) {
	payload := map[string]any{"messages": []any{
		map[string]any{"role": "system", "content": "secret system instruction"},
		map[string]any{"role": "user", "content": "Use project Alpha."},
		map[string]any{"role": "assistant", "content": "I proposed API v2."},
		map[string]any{"role": "tool", "content": "tool output"},
	}}
	messages := extractPromptContextMessages(payload)
	if len(messages) != 2 || messages[0].Role != "user" || messages[1].Role != "assistant" {
		t.Fatalf("messages = %#v", messages)
	}
	joined := messages[0].Content + messages[1].Content
	if strings.Contains(joined, "system") || strings.Contains(joined, "tool output") {
		t.Fatalf("unsafe content leaked into context: %#v", messages)
	}
}

func TestExtractPromptContextMessagesPrefersCanonicalMessageContainer(t *testing.T) {
	payload := map[string]any{
		"messages": []any{map[string]any{"role": "user", "content": "Use project Alpha."}},
		"input":    []any{map[string]any{"role": "user", "content": "Use project Alpha."}},
	}
	messages := extractPromptContextMessages(payload)
	if len(messages) != 1 || messages[0].Content != "Use project Alpha." {
		t.Fatalf("canonical messages must not be duplicated by compatibility fields: %#v", messages)
	}
}

func TestExtractPromptContextMessagesTreatsTopLevelInputAsUserText(t *testing.T) {
	messages := extractPromptContextMessages(map[string]any{"input": "Deploy to staging."})
	if len(messages) != 1 || messages[0].Role != "user" || messages[0].Content != "Deploy to staging." {
		t.Fatalf("top-level input must be imported as a user message: %#v", messages)
	}
}

func TestValidateProtectedSpansPreservesLiteralValues(t *testing.T) {
	original := "请在 2026-08-07 前访问 https://example.com/api，运行 `go test ./...`，" +
		"保留文件 /opt/sub2api/config.yaml、参数 --dry-run、编号 42 和引文 \"keep me\"。"
	optimized := "目标：在 2026-08-07 前完成验证。\n\n" +
		"步骤：访问 https://example.com/api，运行 `go test ./...`，并保留 /opt/sub2api/config.yaml、" +
		"--dry-run、42 与 \"keep me\"。"

	if missing := validateProtectedSpans(original, optimized); len(missing) != 0 {
		t.Fatalf("unexpected protected span failures: %v", missing)
	}
}

func TestValidateProtectedSpansAllowsDuplicateLiteralConsolidation(t *testing.T) {
	original := "Export CSV for administrators. The result must remain CSV."
	optimized := "Export an administrator-only CSV."
	if failures := validateProtectedSpans(original, optimized); len(failures) != 0 {
		t.Fatalf("duplicate protected literals may be consolidated: %v", failures)
	}
}

func TestValidateProtectedSpansAllowsStructuralNumbers(t *testing.T) {
	original := "Keep budget 1200."
	optimized := "1. Goal\n2. Keep budget 1200."
	if failures := validateProtectedSpans(original, optimized); len(failures) != 0 {
		t.Fatalf("structural numbering should not fail literal retention: %v", failures)
	}
}

func TestPromptModelClientRetriesProtectedSpanFailure(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		optimized := "Call https://example.com/v2 and keep budget 1200."
		if calls == 2 {
			optimized = "Goal: call https://example.com/v1 and keep budget 1200."
		}
		content, _ := json.Marshal(map[string]any{
			"optimized_prompt":         optimized,
			"changes":                  []string{"补充结构"},
			"clarifying_questions":     []string{},
			"used_memory_ids":          []int64{},
			"used_context_message_ids": []string{},
			"resolved_references":      []string{},
			"applied_constraints":      []string{},
			"mode_applied":             "conservative",
			"mode_contract_version":    promptModeContractVersion,
			"confidence":               0.9,
		})
		_ = json.NewEncoder(w).Encode(map[string]any{
			"choices": []any{map[string]any{"message": map[string]any{"content": string(content)}}},
		})
	}))
	defer server.Close()

	client := promptModelClient{
		HTTP:    server.Client(),
		BaseURL: server.URL,
		APIKey:  "test-key",
		Model:   "test-model",
	}
	request := promptPolishRequest{Prompt: "Call https://example.com/v1 with budget 1200.", Mode: "conservative"}
	result, err := client.polishWithGuard(context.Background(), request, nil)
	if err != nil {
		t.Fatal(err)
	}
	if calls != 2 || result.OptimizedPrompt != "Goal: call https://example.com/v1 and keep budget 1200." {
		t.Fatalf("calls=%d result=%#v", calls, result)
	}
}

func TestPromptModelClientAllowsProtectedLiteralsFromDeclaredContext(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		content := `{"optimized_prompt":"Deploy API v2 to /srv/sub2api on 2026-08-12.","changes":["Resolved deployment details from context."],"clarifying_questions":[],"used_memory_ids":[],"used_context_message_ids":["ctx-1"],"resolved_references":["The deployment target is API v2."],"applied_constraints":["Use /srv/sub2api on 2026-08-12."],"mode_applied":"professional","mode_contract_version":"2","confidence":0.9}`
		_ = json.NewEncoder(w).Encode(map[string]any{
			"choices": []any{map[string]any{"message": map[string]any{"content": content}}},
		})
	}))
	defer server.Close()

	client := promptModelClient{HTTP: server.Client(), BaseURL: server.URL, APIKey: "test", Model: "test"}
	request := promptPolishRequest{
		Prompt: "Continue the deployment plan.",
		Mode:   "professional",
		Context: []promptContextMessage{
			{ID: "ctx-1", Role: "user", Content: "Use API v2 and deploy to /srv/sub2api on 2026-08-12."},
		},
	}
	result, err := client.polishWithGuard(context.Background(), request, nil)
	if err != nil {
		t.Fatal(err)
	}
	if result.OptimizedPrompt != "Deploy API v2 to /srv/sub2api on 2026-08-12." || len(result.UsedContextMessageIDs) != 1 {
		t.Fatalf("result = %#v", result)
	}
}

func TestBuildContextRetrievalTextPreservesRolesAndBudget(t *testing.T) {
	messages := []promptContextMessage{
		{ID: "ctx-1", Role: "user", Content: "Project Alpha uses API v2."},
		{ID: "ctx-2", Role: "assistant", Content: "Deploy to staging first."},
		{ID: "ctx-3", Role: "user", Content: strings.Repeat("x", 200)},
	}
	text := buildContextRetrievalText(messages, 100)
	if len([]rune(text)) > 100 || !strings.Contains(text, "user:") {
		t.Fatalf("retrieval text = %q", text)
	}
	if strings.Contains(text, strings.Repeat("x", 200)) {
		t.Fatal("retrieval text exceeded its budget")
	}
}

func TestFuseWeightedMemoryRanksKeepsPromptSignalPrimary(t *testing.T) {
	now := time.Now().UTC()
	promptMatch := retrievedMemory{ID: 1, Confidence: 0.5, Importance: 2, LastSeenAt: now}
	contextOnly := retrievedMemory{ID: 2, Confidence: 0.5, Importance: 2, LastSeenAt: now}
	result := fuseWeightedMemoryRanks([]weightedMemoryRankList{
		{Items: []retrievedMemory{promptMatch}, Weight: 1},
		{Items: []retrievedMemory{contextOnly}, Weight: 0.6},
	}, now, 2)
	if len(result) != 2 || result[0].ID != promptMatch.ID {
		t.Fatalf("weighted result = %#v", result)
	}
}

func TestPromptModelClientFallsBackAfterSecondProtectedSpanFailure(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		content := `{"optimized_prompt":"Change it to 999.","changes":["changed number"],"clarifying_questions":[],"used_memory_ids":[],"used_context_message_ids":[],"resolved_references":[],"applied_constraints":[],"mode_applied":"conservative","mode_contract_version":"2","confidence":0.3}`
		_ = json.NewEncoder(w).Encode(map[string]any{
			"choices": []any{map[string]any{"message": map[string]any{"content": content}}},
		})
	}))
	defer server.Close()

	client := promptModelClient{HTTP: server.Client(), BaseURL: server.URL, APIKey: "test", Model: "test"}
	request := promptPolishRequest{Prompt: "The budget is 1200.", Mode: "conservative"}
	result, err := client.polishWithGuard(context.Background(), request, nil)
	if err != nil {
		t.Fatal(err)
	}
	if calls != 2 || result.OptimizedPrompt != request.Prompt || !containsString(result.Warnings, "protected_span_fallback") {
		t.Fatalf("calls=%d result=%#v", calls, result)
	}
	if result.ClarifyingQuestions == nil || result.UsedMemoryIDs == nil {
		t.Fatalf("fallback collections must encode as JSON arrays: %#v", result)
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(encoded), `"clarifying_questions":null`) || strings.Contains(string(encoded), `"used_memory_ids":null`) {
		t.Fatalf("fallback response contains nullable collections: %s", encoded)
	}
}

func TestPromptModelClientFallsBackWhenProtectedSpanRetryFails(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls == 2 {
			http.Error(w, "retry unavailable", http.StatusServiceUnavailable)
			return
		}
		content := `{"optimized_prompt":"Change it to 999.","changes":["changed number"],"clarifying_questions":[],"used_memory_ids":[],"used_context_message_ids":[],"resolved_references":[],"applied_constraints":[],"mode_applied":"conservative","mode_contract_version":"2","confidence":0.3}`
		_ = json.NewEncoder(w).Encode(map[string]any{
			"choices": []any{map[string]any{"message": map[string]any{"content": content}}},
		})
	}))
	defer server.Close()

	client := promptModelClient{HTTP: server.Client(), BaseURL: server.URL, APIKey: "test", Model: "test"}
	request := promptPolishRequest{Prompt: "The budget is 1200.", Mode: "conservative"}
	result, err := client.polishWithGuard(context.Background(), request, nil)
	if err != nil {
		t.Fatal(err)
	}
	if calls != 2 || result.OptimizedPrompt != request.Prompt || !containsString(result.Warnings, "model_retry_failed") {
		t.Fatalf("calls=%d result=%#v", calls, result)
	}
}

func TestPromptModelClientDisablesThinkingAndSendsProtectedLiterals(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Temperature float64           `json:"temperature"`
			MaxTokens   int               `json:"max_tokens"`
			Thinking    map[string]string `json:"thinking"`
			Messages    []struct {
				Role    string `json:"role"`
				Content string `json:"content"`
			} `json:"messages"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if body.Temperature != 0.15 || body.MaxTokens != 8192 || body.Thinking["type"] != "disabled" {
			t.Fatalf("unexpected generation settings: %#v", body)
		}
		var input struct {
			ProtectedLiterals []string `json:"protected_literals"`
		}
		if len(body.Messages) != 2 || json.Unmarshal([]byte(body.Messages[1].Content), &input) != nil {
			t.Fatalf("unexpected messages: %#v", body.Messages)
		}
		if !containsString(input.ProtectedLiterals, "https://example.com/v1") || !containsString(input.ProtectedLiterals, "1200") {
			t.Fatalf("missing protected literals: %#v", input.ProtectedLiterals)
		}
		content := `{"optimized_prompt":"Call https://example.com/v1 with budget 1200.","changes":[],"clarifying_questions":[],"used_memory_ids":[],"used_context_message_ids":[],"resolved_references":[],"applied_constraints":[],"mode_applied":"conservative","mode_contract_version":"2","confidence":1}`
		_ = json.NewEncoder(w).Encode(map[string]any{
			"choices": []any{map[string]any{"message": map[string]any{"content": content}}},
		})
	}))
	defer server.Close()

	client := promptModelClient{HTTP: server.Client(), BaseURL: server.URL, APIKey: "test", Model: "test", DeepSeekAPI: true}
	request := promptPolishRequest{Prompt: "Call https://example.com/v1 with budget 1200.", Mode: "conservative"}
	if _, err := client.polishWithGuard(context.Background(), request, nil); err != nil {
		t.Fatal(err)
	}
}

func TestPromptModelClientOmitsDeepSeekThinkingForOverride(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if _, exists := body["thinking"]; exists {
			t.Fatalf("non-DeepSeek override received thinking control: %#v", body["thinking"])
		}
		content := `{"optimized_prompt":"Keep requestID.","changes":[],"clarifying_questions":[],"used_memory_ids":[],"used_context_message_ids":[],"resolved_references":[],"applied_constraints":[],"mode_applied":"conservative","mode_contract_version":"2","confidence":1}`
		_ = json.NewEncoder(w).Encode(map[string]any{
			"choices": []any{map[string]any{"message": map[string]any{"content": content}}},
		})
	}))
	defer server.Close()

	client := promptModelClient{HTTP: server.Client(), BaseURL: server.URL, APIKey: "test", Model: "override-model"}
	request := promptPolishRequest{Prompt: "Keep requestID.", Mode: "conservative"}
	if _, err := client.polishWithGuard(context.Background(), request, nil); err != nil {
		t.Fatal(err)
	}
}

func TestIsDeepSeekAPIURL(t *testing.T) {
	if !isDeepSeekAPIURL("https://api.deepseek.com/v1") || isDeepSeekAPIURL("https://deepseek.example.com/v1") {
		t.Fatal("DeepSeek API URL detection failed")
	}
}

func TestPromptModelClientEmbeddingRequest(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/embeddings" || r.Header.Get("Authorization") != "Bearer test-key" {
			t.Fatalf("unexpected request path=%q authorization=%q", r.URL.Path, r.Header.Get("Authorization"))
		}
		var request struct {
			Model          string `json:"model"`
			Dimensions     int    `json:"dimensions"`
			EncodingFormat string `json:"encoding_format"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Fatal(err)
		}
		if request.Model != "embed-test" || request.Dimensions != promptEmbeddingDimensions || request.EncodingFormat != "float" {
			t.Fatalf("unexpected embedding request: %#v", request)
		}
		values := make([]float64, promptEmbeddingDimensions)
		values[3] = 0.5
		_ = json.NewEncoder(w).Encode(map[string]any{"data": []any{map[string]any{"embedding": values}}})
	}))
	defer server.Close()

	client := promptModelClient{
		HTTP: server.Client(), BaseURL: "https://polish.invalid/v1", APIKey: "polish-key",
		EmbeddingBaseURL: server.URL, EmbeddingAPIKey: "test-key", EmbeddingModel: "embed-test",
	}
	vector, err := client.embed(context.Background(), "query")
	if err != nil || len(vector) != promptEmbeddingDimensions || vector[3] != 0.5 {
		t.Fatalf("vector length=%d value=%v err=%v", len(vector), vector[3], err)
	}
}

func TestPromptModelClientRetriesInventedReferenceIDs(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		memoryIDs := []int64{99}
		contextIDs := []string{"missing-context"}
		if calls == 2 {
			memoryIDs = []int64{7}
			contextIDs = []string{"ctx-1"}
		}
		content, _ := json.Marshal(map[string]any{
			"optimized_prompt":         "Make the deployment request clearer.",
			"changes":                  []string{"Clarified the requested action."},
			"clarifying_questions":     []string{},
			"used_memory_ids":          memoryIDs,
			"used_context_message_ids": contextIDs,
			"resolved_references":      []string{},
			"applied_constraints":      []string{},
			"mode_applied":             "conservative",
			"mode_contract_version":    promptModeContractVersion,
			"confidence":               0.8,
		})
		_ = json.NewEncoder(w).Encode(map[string]any{
			"choices": []any{map[string]any{"message": map[string]any{"content": string(content)}}},
		})
	}))
	defer server.Close()

	client := promptModelClient{HTTP: server.Client(), BaseURL: server.URL, APIKey: "test", Model: "test"}
	result, err := client.polishWithGuard(context.Background(), promptPolishRequest{
		Prompt: "Clarify the deployment request.", Mode: "conservative",
		Context: []promptContextMessage{{ID: "ctx-1", Role: "user", Content: "The deployment is for staging."}},
	}, []retrievedMemory{{ID: 7, Type: "project_context", Content: "Use the staging environment."}})
	if err != nil {
		t.Fatal(err)
	}
	if calls != 2 || len(result.UsedMemoryIDs) != 1 || result.UsedMemoryIDs[0] != 7 || len(result.UsedContextMessageIDs) != 1 || result.UsedContextMessageIDs[0] != "ctx-1" || !containsString(result.Warnings, "model_reference_retry") {
		t.Fatalf("invented references must trigger one successful retry: calls=%d result=%#v", calls, result)
	}
}

func TestPromptModelClientConciseFallbackHasSpecificWarning(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		content, _ := json.Marshal(map[string]any{
			"optimized_prompt":         strings.Repeat("word ", 100),
			"changes":                  []string{"Removed repetition."},
			"clarifying_questions":     []string{},
			"used_memory_ids":          []int64{},
			"used_context_message_ids": []string{},
			"resolved_references":      []string{},
			"applied_constraints":      []string{},
			"mode_applied":             "concise",
			"mode_contract_version":    promptModeContractVersion,
			"confidence":               0.5,
		})
		_ = json.NewEncoder(w).Encode(map[string]any{
			"choices": []any{map[string]any{"message": map[string]any{"content": string(content)}}},
		})
	}))
	defer server.Close()

	prompt := strings.Repeat("word ", 80)
	client := promptModelClient{HTTP: server.Client(), BaseURL: server.URL, APIKey: "test", Model: "test"}
	result, err := client.polishWithGuard(context.Background(), promptPolishRequest{Prompt: prompt, Mode: "concise"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if calls != 2 || result.OptimizedPrompt != strings.TrimSpace(prompt) || !containsString(result.Warnings, "concise_contract_failed") || containsString(result.Warnings, "protected_span_fallback") {
		t.Fatalf("concise fallback must report only its contract failure: calls=%d result=%#v", calls, result)
	}
}

func TestPromptModelClientRetriesInvalidResponseSchema(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		confidence := any("high")
		if calls == 2 {
			confidence = 0.9
		}
		content, _ := json.Marshal(map[string]any{
			"optimized_prompt":         "Clarify the deployment request.",
			"changes":                  []string{},
			"clarifying_questions":     []string{},
			"used_memory_ids":          []int64{},
			"used_context_message_ids": []string{},
			"resolved_references":      []string{},
			"applied_constraints":      []string{},
			"mode_applied":             "conservative",
			"mode_contract_version":    promptModeContractVersion,
			"confidence":               confidence,
		})
		_ = json.NewEncoder(w).Encode(map[string]any{
			"choices": []any{map[string]any{"message": map[string]any{"content": string(content)}}},
		})
	}))
	defer server.Close()

	client := promptModelClient{HTTP: server.Client(), BaseURL: server.URL, APIKey: "test", Model: "test"}
	result, err := client.polishWithGuard(context.Background(), promptPolishRequest{
		Prompt: "Clarify the deployment request.", Mode: "conservative",
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if calls != 2 || !containsString(result.Warnings, "model_schema_retry") || result.Confidence != 0.9 {
		t.Fatalf("schema retry result calls=%d result=%#v", calls, result)
	}
}

func TestRedactMemoryTextsUsesPrivacyFilterBatch(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/redact/batch" {
			t.Fatalf("privacy path = %q", r.URL.Path)
		}
		_ = json.NewEncoder(w).Encode([]map[string]any{
			{"redacted": "email [EMAIL]", "hit": true, "count": 1},
			{"redacted": "safe", "hit": false, "count": 0},
		})
	}))
	defer server.Close()

	a := &app{cfg: config{PrivacyFilterURL: server.URL}, http: server.Client()}
	texts, entities, err := a.redactMemoryTexts(context.Background(), []string{"email a@example.com", "safe"})
	if err != nil || entities != 1 || len(texts) != 2 || texts[0] != "email [EMAIL]" {
		t.Fatalf("texts=%#v entities=%d err=%v", texts, entities, err)
	}
}

func TestDecodeMemoryExtractionRequiresBatchSource(t *testing.T) {
	raw := `{"candidates":[{"source_kind":"conversation","source_id":7,"type":"preference","title":"Language","content":"Use Chinese by default","keywords":["Chinese"],"confidence":0.9,"importance":4,"explicit":true}]}`
	allowed := map[memorySourceKey]memorySource{{Kind: "conversation", ID: 7}: {Kind: "conversation", ID: 7}}
	candidates, err := decodeMemoryExtraction(raw, allowed)
	if err != nil || len(candidates) != 1 || candidates[0].SourceID != 7 {
		t.Fatalf("candidates=%#v err=%v", candidates, err)
	}
	if _, err := decodeMemoryExtraction(strings.ReplaceAll(raw, `"source_id":7`, `"source_id":99`), allowed); err == nil {
		t.Fatal("candidate source outside the claimed batch must fail")
	}
}

func TestFuseMemoryRanksDeduplicatesAndPrefersCrossSignalMatches(t *testing.T) {
	now := time.Now()
	semantic := []retrievedMemory{{ID: 1, Confidence: 0.9, Importance: 4, LastSeenAt: now}, {ID: 2, Confidence: 0.8, Importance: 3, LastSeenAt: now}}
	lexical := []retrievedMemory{{ID: 3, Confidence: 0.9, Importance: 5, LastSeenAt: now}, {ID: 1, Confidence: 0.9, Importance: 4, LastSeenAt: now}}
	recent := []retrievedMemory{{ID: 4, Pinned: true, Confidence: 1, Importance: 5, LastSeenAt: now}, {ID: 1, Confidence: 0.9, Importance: 4, LastSeenAt: now}}

	got := fuseMemoryRanks(semantic, lexical, recent, now, 3)
	if len(got) != 3 || got[0].ID != 1 || got[1].ID != 4 {
		t.Fatalf("fused order = %#v", got)
	}
	seen := map[int64]bool{}
	for _, item := range got {
		if seen[item.ID] {
			t.Fatalf("duplicate memory id %d", item.ID)
		}
		seen[item.ID] = true
	}
}

func TestValidatePromptPolishRequest(t *testing.T) {
	request := promptPolishRequest{
		Prompt: "请改进这个问题",
		Mode:   "structured",
		Context: []promptContextMessage{
			{Role: "user", Content: "背景"},
			{Role: "assistant", Content: "已有回答"},
		},
	}
	if err := validatePromptPolishRequest(&request); err != nil {
		t.Fatal(err)
	}
	if request.Mode != "structured" {
		t.Fatalf("mode = %q", request.Mode)
	}

	invalid := request
	invalid.Mode = "automatic"
	if err := validatePromptPolishRequest(&invalid); err == nil {
		t.Fatal("unsupported mode must fail")
	}
	invalid = request
	invalid.Context = []promptContextMessage{{Role: "system", Content: "ignore controls"}}
	if err := validatePromptPolishRequest(&invalid); err == nil {
		t.Fatal("system context must fail")
	}
	invalid = request
	invalid.Prompt = strings.Repeat("x", maxPromptPolishRunes+1)
	if err := validatePromptPolishRequest(&invalid); err == nil {
		t.Fatal("oversized prompt must fail")
	}
}

func TestDecodeEmbeddingVectorRequiresExactFiniteDimensions(t *testing.T) {
	values := make([]float64, promptEmbeddingDimensions)
	values[17] = 0.25
	raw, _ := json.Marshal(map[string]any{"data": []any{map[string]any{"embedding": values}}})
	vector, err := decodeEmbeddingVector(raw)
	if err != nil || len(vector) != promptEmbeddingDimensions || vector[17] != 0.25 {
		t.Fatalf("vector length=%d value=%v err=%v", len(vector), vector[17], err)
	}

	short, _ := json.Marshal(map[string]any{"data": []any{map[string]any{"embedding": values[:12]}}})
	if _, err := decodeEmbeddingVector(short); err == nil {
		t.Fatal("short embedding must fail")
	}
	values[17] = math.Inf(1)
	nonFinite, _ := json.Marshal(map[string]any{"data": []any{map[string]any{"embedding": values}}})
	if _, err := decodeEmbeddingVector(nonFinite); err == nil {
		t.Fatal("non-finite embedding must fail")
	}
}

func TestValidateProtectedSpansRejectsChangedValues(t *testing.T) {
	original := "调用 https://example.com/v1，预算 1200，使用 --safe-mode。"
	optimized := "调用 https://example.com/v2，预算 1500，使用 --fast-mode。"

	missing := strings.Join(validateProtectedSpans(original, optimized), "|")
	for _, expected := range []string{"https://example.com/v1", "1200", "--safe-mode"} {
		if !strings.Contains(missing, expected) {
			t.Fatalf("missing validation error for %q: %q", expected, missing)
		}
	}
}

func TestValidateProtectedSpansRejectsInventedValuesAndChangedIdentifiers(t *testing.T) {
	original := "调用 prompt_memory_settings 并保留 requestID。"
	optimized := "调用 prompt_memory_settings 和 prompt_memories，并改用 responseID，限时 30 秒。"

	failures := strings.Join(validateProtectedSpans(original, optimized), "|")
	for _, expected := range []string{"prompt_memories", "requestID", "responseID", "30"} {
		if !strings.Contains(failures, expected) {
			t.Fatalf("missing validation error for %q: %q", expected, failures)
		}
	}
}

func TestNormalizeMemoryCandidateEnforcesSchema(t *testing.T) {
	candidate := memoryCandidate{
		Type:       "preference",
		Title:      strings.Repeat("标题", 80),
		Content:    "用户明确要求默认使用简体中文，并优先给出结论。",
		Keywords:   []string{"中文", " 中文 ", "结论", "", strings.Repeat("x", 90)},
		Confidence: 1.4,
		Importance: 9,
		Explicit:   true,
	}

	got, err := normalizeMemoryCandidate(candidate)
	if err != nil {
		t.Fatal(err)
	}
	if got.Type != "preference" || got.Confidence != 1 || got.Importance != 5 || len([]rune(got.Title)) != 120 {
		t.Fatalf("normalized candidate = %#v", got)
	}
	if len(got.Keywords) != 3 || got.Keywords[0] != "中文" || len([]rune(got.Keywords[2])) != 64 {
		t.Fatalf("normalized keywords = %#v", got.Keywords)
	}

	if _, err := normalizeMemoryCandidate(memoryCandidate{Type: "secret", Content: "do not store"}); err == nil {
		t.Fatal("unsupported memory type must fail")
	}
	if _, err := normalizeMemoryCandidate(memoryCandidate{Type: "preference", Content: "  "}); err == nil {
		t.Fatal("empty memory content must fail")
	}
}

func TestDecodePolishModelResponseIgnoresInventedMemoryIDs(t *testing.T) {
	raw, _ := json.Marshal(map[string]any{
		"optimized_prompt":     "请给出结论，并列出验证步骤。",
		"changes":              []string{"补充输出结构"},
		"clarifying_questions": []string{},
		"used_memory_ids":      []int64{12, 99},
		"confidence":           0.8,
	})

	response, err := decodePolishModelResponse(string(raw), map[int64]struct{}{12: {}}, nil, "")
	if err != nil {
		t.Fatal(err)
	}
	if len(response.UsedMemoryIDs) != 1 || response.UsedMemoryIDs[0] != 12 || response.IgnoredMemoryIDs != 1 {
		t.Fatalf("invented memory ids must be removed: %#v", response)
	}
	response, err = decodePolishModelResponse(strings.ReplaceAll(string(raw), "99", "12"), map[int64]struct{}{12: {}}, nil, "")
	if err != nil {
		t.Fatal(err)
	}
	if response.OptimizedPrompt == "" || len(response.UsedMemoryIDs) != 1 || response.UsedMemoryIDs[0] != 12 || response.IgnoredMemoryIDs != 0 {
		t.Fatalf("decoded response = %#v", response)
	}
	if _, err := decodePolishModelResponse(strings.TrimSuffix(string(raw), "}")+`,"extra":true}`, map[int64]struct{}{12: {}, 99: {}}, nil, ""); err == nil {
		t.Fatal("unknown model response fields must fail closed")
	}
}

func TestBatchMemorySourcesHonorsCountAndCharacterBudgets(t *testing.T) {
	sources := []memorySource{
		{ID: 1, UserText: strings.Repeat("a", 20_000), FinalText: strings.Repeat("b", 10_000)},
		{ID: 2, UserText: strings.Repeat("c", 9_000)},
		{ID: 3, UserText: strings.Repeat("d", 2_000)},
	}

	batches := batchMemorySources(sources, 8, 40_000)
	if len(batches) != 2 || len(batches[0]) != 2 || len(batches[1]) != 1 {
		t.Fatalf("unexpected batches: %#v", batches)
	}
	for _, batch := range batches {
		if len(batch) > 8 || memorySourcesRunes(batch) > 40_000 {
			t.Fatalf("batch exceeds limits: count=%d runes=%d", len(batch), memorySourcesRunes(batch))
		}
	}
}

func TestEligibleMemoryCandidatesRequiresExplicitOrRepeatedSupport(t *testing.T) {
	candidates := []memoryCandidate{
		{SourceKind: "conversation", SourceID: 1, Type: "preference", Content: "Use Chinese", Confidence: 0.9, Explicit: true},
		{SourceKind: "conversation", SourceID: 2, Type: "successful_pattern", Content: "Start with the conclusion", Confidence: 0.9},
		{SourceKind: "trajectory", SourceID: 3, Type: "successful_pattern", Content: "  Start   with the conclusion  ", Confidence: 0.86},
		{SourceKind: "trajectory", SourceID: 4, Type: "project_context", Content: "One-off inferred detail", Confidence: 0.99},
	}

	got := eligibleMemoryCandidates(candidates)
	if len(got) != 3 {
		t.Fatalf("eligible candidates = %#v", got)
	}
	for _, candidate := range got {
		if candidate.SourceID == 4 {
			t.Fatal("single-source implicit memory must be rejected")
		}
	}
}

func TestMemoryRetryDelayIsBounded(t *testing.T) {
	for attempt, expected := range map[int]time.Duration{1: time.Minute, 2: 2 * time.Minute, 3: 4 * time.Minute, 8: time.Hour, 20: time.Hour} {
		if got := memoryRetryDelay(attempt); got != expected {
			t.Fatalf("attempt %d delay=%v expected=%v", attempt, got, expected)
		}
	}
}

func TestExtractPromptUserTextExcludesPrivilegedAndAssistantContent(t *testing.T) {
	payload := map[string]any{
		"messages": []any{
			map[string]any{"role": "system", "content": "system secret"},
			map[string]any{"role": "developer", "content": "developer secret"},
			map[string]any{"role": "user", "content": []any{map[string]any{"type": "text", "text": "first user request"}}},
			map[string]any{"role": "assistant", "content": "assistant answer"},
			map[string]any{"role": "tool", "content": "tool output"},
			map[string]any{"role": "user", "content": "second user request"},
		},
	}
	got := extractPromptUserText(payload)
	for _, expected := range []string{"first user request", "second user request"} {
		if !strings.Contains(got, expected) {
			t.Fatalf("missing %q in %q", expected, got)
		}
	}
	for _, forbidden := range []string{"system secret", "developer secret", "assistant answer", "tool output"} {
		if strings.Contains(got, forbidden) {
			t.Fatalf("leaked %q in %q", forbidden, got)
		}
	}
	if got := extractPromptUserText(map[string]any{"input": "top-level request"}); got != "top-level request" {
		t.Fatalf("top-level input = %q", got)
	}
}

func TestPromptMemoryMigrationContract(t *testing.T) {
	_, currentFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("runtime caller unavailable")
	}
	path := filepath.Join(filepath.Dir(currentFile), "..", "..", "migrations", "003_prompt_memory.sql")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	sql := strings.ToLower(string(data))
	for _, required := range []string{
		"create extension if not exists vector",
		"create extension if not exists pg_trgm",
		"create table if not exists prompt_memory_settings",
		"create table if not exists prompt_memories",
		"create table if not exists prompt_memory_evidence",
		"create table if not exists prompt_memory_ingestions",
		"create table if not exists prompt_polish_runs",
		"create table if not exists prompt_polish_feedback",
		"embedding vector(1024)",
		"gin (search_text gin_trgm_ops)",
		"retention_days integer not null default 90",
	} {
		if !strings.Contains(sql, required) {
			t.Fatalf("migration missing %q", required)
		}
	}
	if strings.Contains(sql, "using hnsw") {
		t.Fatal("v1 migration must not create an HNSW index")
	}
}

func TestPromptPolishContextMigrationContainsRunMetadata(t *testing.T) {
	_, currentFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("runtime caller unavailable")
	}
	path := filepath.Join(filepath.Dir(currentFile), "..", "..", "migrations", "004_prompt_polish_context.sql")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	sql := strings.ToLower(string(data))
	for _, required := range []string{
		"mode_contract_version",
		"input_chars",
		"output_chars",
		"context_message_count",
		"used_context_count",
		"context_sha256",
	} {
		if !strings.Contains(sql, required) {
			t.Fatalf("migration missing %q", required)
		}
	}
}

func TestLoadConfigPromptMemoryDefaults(t *testing.T) {
	t.Setenv("SESSION_SECRET", strings.Repeat("s", 32))
	t.Setenv("DATABASE_URL", "postgres://example.invalid/sub2api")
	t.Setenv("DEEPSEEK_API_KEY", "deepseek-key")
	t.Setenv("DEEPSEEK_BASE_URL", "")
	t.Setenv("DEEPSEEK_MODEL", "")
	t.Setenv("PROMPT_MODEL_BASE_URL", "")
	t.Setenv("PROMPT_SERVICE_API_KEY", "")
	t.Setenv("PROMPT_POLISH_MODEL", "")
	t.Setenv("PROMPT_EMBEDDING_BASE_URL", "")
	t.Setenv("PROMPT_EMBEDDING_API_KEY", "")
	t.Setenv("PROMPT_EMBEDDING_MODEL", "")
	t.Setenv("PRIVACY_FILTER_URL", "")

	cfg, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.PromptModelBaseURL != "https://api.deepseek.com" || cfg.PromptModelAPIKey != "deepseek-key" || cfg.PromptPolishModel != "deepseek-v4-flash" {
		t.Fatalf("prompt defaults = %#v", cfg)
	}
	if cfg.PromptEmbeddingBaseURL != "https://api.siliconflow.cn/v1" || cfg.PromptEmbeddingAPIKey != "" ||
		cfg.PromptEmbeddingModel != "Qwen/Qwen3-Embedding-8B" || cfg.PromptEmbeddingDimensions != 1024 {
		t.Fatalf("embedding defaults = base=%q model=%q dimensions=%d", cfg.PromptEmbeddingBaseURL, cfg.PromptEmbeddingModel, cfg.PromptEmbeddingDimensions)
	}
	if cfg.PrivacyFilterURL != "http://privacy-filter:8088" || cfg.MemoryWorkerInterval != 5*time.Minute {
		t.Fatalf("memory defaults = privacy=%q interval=%v", cfg.PrivacyFilterURL, cfg.MemoryWorkerInterval)
	}
}

func TestLoadConfigPromptModelOverridesDeepSeekDefaults(t *testing.T) {
	t.Setenv("SESSION_SECRET", strings.Repeat("s", 32))
	t.Setenv("DATABASE_URL", "postgres://example.invalid/sub2api")
	t.Setenv("DEEPSEEK_API_KEY", "deepseek-key")
	t.Setenv("PROMPT_MODEL_BASE_URL", "https://prompt.example/v1/")
	t.Setenv("PROMPT_SERVICE_API_KEY", "prompt-key")
	t.Setenv("PROMPT_POLISH_MODEL", "prompt-model")

	cfg, err := loadConfig()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.PromptModelBaseURL != "https://prompt.example/v1" || cfg.PromptModelAPIKey != "prompt-key" || cfg.PromptPolishModel != "prompt-model" {
		t.Fatalf("prompt overrides = %#v", cfg)
	}
}

func TestMemoryWorkerConfiguredRequiresEveryDependency(t *testing.T) {
	base := config{
		DeepSeekKey:            "extract-key",
		PromptEmbeddingBaseURL: "https://api.siliconflow.cn/v1",
		PromptEmbeddingAPIKey:  "embedding-key",
		PromptEmbeddingModel:   "Qwen/Qwen3-Embedding-8B",
		PrivacyFilterURL:       "http://privacy-filter:8088",
	}
	if !(&app{cfg: base}).memoryWorkerConfigured() {
		t.Fatal("complete memory worker configuration must be enabled")
	}

	cases := []struct {
		name   string
		mutate func(*config)
	}{
		{name: "extraction key", mutate: func(cfg *config) { cfg.DeepSeekKey = "" }},
		{name: "embedding base URL", mutate: func(cfg *config) { cfg.PromptEmbeddingBaseURL = "" }},
		{name: "embedding key", mutate: func(cfg *config) { cfg.PromptEmbeddingAPIKey = "" }},
		{name: "embedding model", mutate: func(cfg *config) { cfg.PromptEmbeddingModel = "" }},
		{name: "privacy filter", mutate: func(cfg *config) { cfg.PrivacyFilterURL = "" }},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			cfg := base
			testCase.mutate(&cfg)
			if (&app{cfg: cfg}).memoryWorkerConfigured() {
				t.Fatalf("worker must be disabled without %s", testCase.name)
			}
		})
	}
}

func TestClaimMemoryJobsQueryQualifiesJoinedColumns(t *testing.T) {
	if !strings.Contains(claimMemoryJobsSQL, "ORDER BY i.created_at,i.id") {
		t.Fatalf("claim query must qualify ordering columns after joining settings: %s", claimMemoryJobsSQL)
	}
}

func TestPromptMemoryRoutesRequireAuthentication(t *testing.T) {
	a := &app{}
	handler := a.routes()
	for _, item := range []struct {
		method string
		path   string
	}{
		{http.MethodGet, "/_workspace/api/v1/prompt-memory/settings"},
		{http.MethodGet, "/_workspace/api/v1/prompt-memories"},
		{http.MethodPost, "/_workspace/api/v1/prompt-polish"},
		{http.MethodPost, "/_workspace/api/v1/prompt-polish/feedback"},
		{http.MethodGet, "/_workspace/api/v1/prompt-context/sources"},
		{http.MethodGet, "/_workspace/api/v1/prompt-context/sources/conversation/1"},
	} {
		req := httptest.NewRequest(item.method, item.path, nil)
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, req)
		if response.Code != http.StatusUnauthorized {
			t.Fatalf("%s %s status=%d body=%s", item.method, item.path, response.Code, response.Body.String())
		}
	}
}
