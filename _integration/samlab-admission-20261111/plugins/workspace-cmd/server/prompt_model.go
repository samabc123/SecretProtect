package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type retrievedMemory struct {
	ID         int64     `json:"id"`
	Type       string    `json:"type"`
	Title      string    `json:"title"`
	Content    string    `json:"content"`
	Confidence float64   `json:"confidence"`
	Importance int       `json:"importance"`
	Pinned     bool      `json:"pinned"`
	LastSeenAt time.Time `json:"last_seen_at"`
	Score      float64   `json:"-"`
}

type promptPolishResult struct {
	polishModelResponse
	Warnings []string `json:"warnings"`
}

var errInvalidPromptModelResponse = errors.New("prompt model returned an invalid response")

type promptModelClient struct {
	HTTP             *http.Client
	BaseURL          string
	APIKey           string
	Model            string
	EmbeddingBaseURL string
	EmbeddingAPIKey  string
	EmbeddingModel   string
	DeepSeekAPI      bool
}

func (c promptModelClient) embed(ctx context.Context, input string) ([]float64, error) {
	if c.HTTP == nil {
		c.HTTP = &http.Client{Timeout: 20 * time.Second}
	}
	if strings.TrimSpace(c.EmbeddingBaseURL) == "" || strings.TrimSpace(c.EmbeddingModel) == "" {
		return nil, errors.New("embedding model is not configured")
	}
	body, _ := json.Marshal(map[string]any{
		"model":           c.EmbeddingModel,
		"input":           input,
		"dimensions":      promptEmbeddingDimensions,
		"encoding_format": "float",
	})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(c.EmbeddingBaseURL, "/")+"/embeddings", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.EmbeddingAPIKey != "" {
		req.Header.Set("Authorization", "Bearer "+c.EmbeddingAPIKey)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("embedding model status %d: %s", resp.StatusCode, truncateRunes(string(data), 300))
	}
	return decodeEmbeddingVector(data)
}

func (c promptModelClient) polishWithGuard(ctx context.Context, request promptPolishRequest, memories []retrievedMemory) (promptPolishResult, error) {
	if err := validatePromptPolishRequest(&request); err != nil {
		return promptPolishResult{}, err
	}
	var lastFailures []string
	schemaRetried := false
	referenceRetried := false
	lastProtectedFailure := false
	lastReferenceFailure := false
	lastModeFailure := ""
	for attempt := 0; attempt < 2; attempt++ {
		response, err := c.polishOnce(ctx, request, memories, lastFailures, schemaRetried)
		if err != nil {
			if attempt == 0 && errors.Is(err, errInvalidPromptModelResponse) {
				schemaRetried = true
				continue
			}
			if len(lastFailures) > 0 {
				warnings := polishFallbackWarnings(request.Mode, lastProtectedFailure, lastReferenceFailure, lastModeFailure)
				warnings = append(warnings, "model_retry_failed")
				return polishContractFallback(request.Prompt, request.Mode, warnings...), nil
			}
			return promptPolishResult{}, err
		}
		evidence := usedPolishEvidence(request.Context, memories, response.UsedContextMessageIDs, response.UsedMemoryIDs)
		protectedFailures := validateProtectedSpansWithEvidence(request.Prompt, response.OptimizedPrompt, evidence)
		lastProtectedFailure = len(protectedFailures) > 0
		lastReferenceFailure = response.IgnoredMemoryIDs > 0 || response.IgnoredContextIDs > 0
		lastModeFailure = validatePolishModeContract(request, response)
		lastFailures = append(lastFailures[:0], protectedFailures...)
		if response.IgnoredMemoryIDs > 0 {
			lastFailures = append(lastFailures, fmt.Sprintf("used_memory_ids contains %d unavailable IDs", response.IgnoredMemoryIDs))
		}
		if response.IgnoredContextIDs > 0 {
			lastFailures = append(lastFailures, fmt.Sprintf("used_context_message_ids contains %d unavailable IDs", response.IgnoredContextIDs))
		}
		if lastModeFailure != "" {
			lastFailures = append(lastFailures, lastModeFailure)
		}
		if len(lastFailures) == 0 {
			warnings := []string{}
			if schemaRetried {
				warnings = append(warnings, "model_schema_retry")
			}
			if referenceRetried {
				warnings = append(warnings, "model_reference_retry")
			}
			return promptPolishResult{polishModelResponse: response, Warnings: warnings}, nil
		}
		if lastReferenceFailure {
			referenceRetried = true
		}
	}
	warnings := polishFallbackWarnings(request.Mode, lastProtectedFailure, lastReferenceFailure, lastModeFailure)
	return polishContractFallback(request.Prompt, request.Mode, warnings...), nil
}

func polishFallbackWarnings(mode string, protectedFailure, referenceFailure bool, modeFailure string) []string {
	warnings := make([]string, 0, 3)
	if protectedFailure {
		warnings = append(warnings, "protected_span_fallback")
	}
	if referenceFailure {
		warnings = append(warnings, "invalid_reference_fallback")
	}
	if modeFailure != "" {
		if mode == "concise" && strings.Contains(modeFailure, "concise contract") {
			warnings = append(warnings, "concise_contract_failed")
		} else {
			warnings = append(warnings, "mode_contract_fallback")
		}
	}
	if len(warnings) == 0 {
		warnings = append(warnings, "mode_contract_fallback")
	}
	return warnings
}

func polishContractFallback(prompt, mode string, warnings ...string) promptPolishResult {
	return promptPolishResult{
		polishModelResponse: polishModelResponse{
			OptimizedPrompt:       prompt,
			Changes:               []string{"The model output did not satisfy the rewrite contract, so the original prompt was returned."},
			ClarifyingQuestions:   []string{},
			UsedMemoryIDs:         []int64{},
			UsedContextMessageIDs: []string{},
			ResolvedReferences:    []string{},
			AppliedConstraints:    []string{},
			ModeApplied:           mode,
			ModeContractVersion:   promptModeContractVersion,
			Confidence:            0,
		},
		Warnings: uniqueStrings(warnings),
	}
}

func protectedSpanFallback(prompt, mode string, extraWarnings ...string) promptPolishResult {
	warnings := append([]string{"protected_span_fallback"}, extraWarnings...)
	return polishContractFallback(prompt, mode, warnings...)
}

func (c promptModelClient) polishOnce(ctx context.Context, request promptPolishRequest, memories []retrievedMemory, retryFailures []string, retryInvalidSchema bool) (polishModelResponse, error) {
	if c.HTTP == nil {
		c.HTTP = &http.Client{Timeout: 20 * time.Second}
	}
	if strings.TrimSpace(c.BaseURL) == "" || strings.TrimSpace(c.Model) == "" {
		return polishModelResponse{}, errors.New("prompt model is not configured")
	}
	allowedMemoryIDs := make(map[int64]struct{}, len(memories))
	for _, memory := range memories {
		allowedMemoryIDs[memory.ID] = struct{}{}
	}
	allowedContextIDs := make(map[string]struct{}, len(request.Context))
	for _, message := range request.Context {
		allowedContextIDs[message.ID] = struct{}{}
	}
	system, err := buildPolishSystemPrompt(request.Mode)
	if err != nil {
		return polishModelResponse{}, err
	}
	input := map[string]any{
		"mode":                      request.Mode,
		"current_prompt":            request.Prompt,
		"current_context":           request.Context,
		"memories":                  memories,
		"protected_literals":        protectedSpans(request.Prompt),
		"previous_optimized_prompt": request.PreviousOptimizedPrompt,
		"iteration_feedback":        request.IterationFeedback,
	}
	if len(retryFailures) > 0 {
		input["retry_requirement"] = "The prior rewrite violated these requirements. Correct every item without answering the request: " + strings.Join(retryFailures, "; ")
	} else if retryInvalidSchema {
		input["retry_requirement"] = "The prior response violated the JSON schema. Return every required field exactly as specified by the system prompt."
	}
	inputJSON, _ := json.Marshal(input)
	body := map[string]any{
		"model":           c.Model,
		"temperature":     0.15,
		"max_tokens":      8192,
		"response_format": map[string]string{"type": "json_object"},
		"messages": []map[string]string{
			{"role": "system", "content": system},
			{"role": "user", "content": string(inputJSON)},
		},
	}
	if c.DeepSeekAPI {
		body["thinking"] = map[string]string{"type": "disabled"}
	}
	rawBody, _ := json.Marshal(body)
	endpoint := strings.TrimRight(c.BaseURL, "/") + "/chat/completions"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(rawBody))
	if err != nil {
		return polishModelResponse{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+c.APIKey)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return polishModelResponse{}, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return polishModelResponse{}, err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return polishModelResponse{}, fmt.Errorf("prompt model status %d: %s", resp.StatusCode, truncateRunes(string(data), 300))
	}
	var payload struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
		Usage struct {
			PromptTokens     int `json:"prompt_tokens"`
			CompletionTokens int `json:"completion_tokens"`
		} `json:"usage"`
	}
	if err := json.Unmarshal(data, &payload); err != nil || len(payload.Choices) == 0 {
		return polishModelResponse{}, fmt.Errorf("%w: invalid completion envelope", errInvalidPromptModelResponse)
	}
	result, err := decodePolishModelResponse(payload.Choices[0].Message.Content, allowedMemoryIDs, allowedContextIDs, request.Mode)
	if err != nil {
		return polishModelResponse{}, fmt.Errorf("%w: %v", errInvalidPromptModelResponse, err)
	}
	result.PromptTokens = payload.Usage.PromptTokens
	result.CompletionTokens = payload.Usage.CompletionTokens
	return result, nil
}

func usedPolishEvidence(contextMessages []promptContextMessage, memories []retrievedMemory, contextIDs []string, memoryIDs []int64) []string {
	usedContext := make(map[string]struct{}, len(contextIDs))
	for _, id := range contextIDs {
		usedContext[id] = struct{}{}
	}
	usedMemory := make(map[int64]struct{}, len(memoryIDs))
	for _, id := range memoryIDs {
		usedMemory[id] = struct{}{}
	}
	evidence := make([]string, 0, len(contextIDs)+len(memoryIDs))
	for _, message := range contextMessages {
		if _, ok := usedContext[message.ID]; ok {
			evidence = append(evidence, message.Content)
		}
	}
	for _, memory := range memories {
		if _, ok := usedMemory[memory.ID]; ok {
			evidence = append(evidence, memory.Title+"\n"+memory.Content)
		}
	}
	return evidence
}

func validatePolishModeContract(request promptPolishRequest, response polishModelResponse) string {
	if request.Mode != "concise" {
		return ""
	}
	inputLength := len([]rune(request.Prompt))
	outputLength := len([]rune(response.OptimizedPrompt))
	if inputLength > 300 && float64(outputLength) > float64(inputLength)*0.75 {
		return fmt.Sprintf("concise contract requires output length at most 75 percent of input (%d > %d)", outputLength, inputLength*75/100)
	}
	if inputLength >= 120 && inputLength <= 300 && float64(outputLength) > float64(inputLength)*0.90 {
		return fmt.Sprintf("concise contract requires output length at most 90 percent of input (%d > %d)", outputLength, inputLength*90/100)
	}
	return ""
}

func containsFailure(values []string, expected string) bool {
	for _, value := range values {
		if strings.Contains(value, expected) {
			return true
		}
	}
	return false
}

func isDeepSeekAPIURL(baseURL string) bool {
	parsed, err := url.Parse(strings.TrimSpace(baseURL))
	if err != nil {
		return false
	}
	host := strings.ToLower(parsed.Hostname())
	return host == "deepseek.com" || strings.HasSuffix(host, ".deepseek.com")
}

func containsString(values []string, expected string) bool {
	for _, value := range values {
		if value == expected {
			return true
		}
	}
	return false
}
