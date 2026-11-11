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
)

var (
	errInvalidJSON  = errors.New("conversation request is not valid JSON")
	errBodyTooLarge = errors.New("conversation request exceeds privacy filter limit")
)

type privacyStats struct {
	Texts    int
	HitTexts int
	Entities int
}

type privacyClient struct {
	baseURL *url.URL
	client  *http.Client
	maxBody int64
}

type privacyBatchResponse struct {
	Redacted string `json:"redacted"`
	Hit      bool   `json:"hit"`
	Count    int    `json:"count"`
}

func newPrivacyClient(cfg config) *privacyClient {
	return &privacyClient{
		baseURL: cfg.PrivacyFilterURL,
		client: &http.Client{
			Timeout:   cfg.PrivacyTimeout,
			Transport: &http.Transport{Proxy: nil},
		},
		maxBody: cfg.PrivacyMaxBody,
	}
}

func (p *privacyClient) health(ctx context.Context) error {
	if p == nil || p.baseURL == nil {
		return fmt.Errorf("privacy filter is not configured")
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(p.baseURL.String(), "/")+"/health", nil)
	if err != nil {
		return err
	}
	response, err := p.client.Do(request)
	if err != nil {
		return fmt.Errorf("privacy filter health request failed: %w", err)
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("privacy filter health status %d", response.StatusCode)
	}
	results, err := p.redactBatch(ctx, []string{"archive-plugin-health-probe"})
	if err != nil {
		return fmt.Errorf("privacy filter contract probe failed: %w", err)
	}
	if len(results) != 1 {
		return fmt.Errorf("privacy filter contract probe returned %d results", len(results))
	}
	return nil
}

func readConversationRequest(body io.ReadCloser, maxBody int64) ([]byte, error) {
	if body == nil {
		return []byte{}, nil
	}
	defer func() { _ = body.Close() }()
	limit := maxBody
	if limit <= 0 {
		limit = 268435456
	}
	raw, err := io.ReadAll(io.LimitReader(body, limit+1))
	if err != nil {
		return nil, fmt.Errorf("read conversation body: %w", err)
	}
	if int64(len(raw)) > limit {
		return nil, errBodyTooLarge
	}
	return raw, nil
}

func validateConversationJSON(raw []byte) error {
	if len(bytes.TrimSpace(raw)) == 0 {
		return nil
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var document any
	if err := decoder.Decode(&document); err != nil {
		return errInvalidJSON
	}
	if decoder.Decode(&struct{}{}) != io.EOF {
		return errInvalidJSON
	}
	return nil
}

func (p *privacyClient) redactJSON(ctx context.Context, raw []byte) ([]byte, privacyStats, error) {
	if p == nil || p.baseURL == nil {
		return nil, privacyStats{}, fmt.Errorf("privacy filter is not configured")
	}
	if int64(len(raw)) > p.maxBody && p.maxBody > 0 {
		return nil, privacyStats{}, errBodyTooLarge
	}
	if len(bytes.TrimSpace(raw)) == 0 {
		return raw, privacyStats{}, nil
	}

	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var document any
	if err := decoder.Decode(&document); err != nil {
		return nil, privacyStats{}, errInvalidJSON
	}
	if decoder.Decode(&struct{}{}) != io.EOF {
		return nil, privacyStats{}, errInvalidJSON
	}

	sanitized := sanitizePostgresJSONStrings(document)
	targets := collectPrivacyTargets(document)
	stats := privacyStats{Texts: len(targets)}
	if len(targets) == 0 {
		if !sanitized {
			return raw, stats, nil
		}
		filtered, err := json.Marshal(document)
		if err != nil {
			return nil, privacyStats{}, fmt.Errorf("encode sanitized conversation: %w", err)
		}
		return filtered, stats, nil
	}
	texts := make([]string, len(targets))
	for index := range targets {
		texts[index] = targets[index].value
	}
	results, err := p.redactBatch(ctx, texts)
	if err != nil {
		return nil, privacyStats{}, err
	}
	if len(results) != len(targets) {
		return nil, privacyStats{}, fmt.Errorf("privacy filter returned %d results for %d texts", len(results), len(targets))
	}
	for index, result := range results {
		targets[index].set(result.Redacted)
		if result.Hit {
			stats.HitTexts++
		}
		stats.Entities += result.Count
	}
	sanitizePostgresJSONStrings(document)
	filtered, err := json.Marshal(document)
	if err != nil {
		return nil, privacyStats{}, fmt.Errorf("encode filtered conversation: %w", err)
	}
	return filtered, stats, nil
}

func (p *privacyClient) redactText(ctx context.Context, text string) (string, privacyStats, error) {
	if p == nil || p.baseURL == nil {
		return "", privacyStats{}, fmt.Errorf("privacy filter is not configured")
	}
	text = sanitizePostgresText(text)
	if text == "" {
		return "", privacyStats{}, nil
	}
	results, err := p.redactBatch(ctx, []string{text})
	if err != nil {
		return "", privacyStats{}, err
	}
	if len(results) != 1 {
		return "", privacyStats{}, fmt.Errorf("privacy filter returned %d results for one text", len(results))
	}
	stats := privacyStats{Texts: 1, Entities: results[0].Count}
	if results[0].Hit {
		stats.HitTexts = 1
	}
	return sanitizePostgresText(results[0].Redacted), stats, nil
}

func (p *privacyClient) redactArchive(ctx context.Context, item completedArchive) (completedArchive, error) {
	var total privacyStats
	requestPayload, stats, err := p.redactJSON(ctx, item.RequestPayload)
	if err != nil {
		return completedArchive{}, fmt.Errorf("redact request payload: %w", err)
	}
	total.add(stats)
	assistantText, stats, err := p.redactText(ctx, item.AssistantText)
	if err != nil {
		return completedArchive{}, fmt.Errorf("redact assistant text: %w", err)
	}
	total.add(stats)
	toolCalls, stats, err := p.redactJSON(ctx, item.ToolCalls)
	if err != nil {
		return completedArchive{}, fmt.Errorf("redact tool calls: %w", err)
	}
	total.add(stats)
	finalResponse, stats, err := p.redactJSON(ctx, item.FinalResponse)
	if err != nil {
		return completedArchive{}, fmt.Errorf("redact final response: %w", err)
	}
	total.add(stats)
	item.RequestPayload = requestPayload
	item.RequestID = sanitizePostgresText(item.RequestID)
	item.Protocol = sanitizePostgresText(item.Protocol)
	item.Endpoint = sanitizePostgresText(item.Endpoint)
	item.Model = sanitizePostgresText(item.Model)
	item.AssistantText = sanitizePostgresText(assistantText)
	item.ToolCalls = toolCalls
	item.FinalResponse = finalResponse
	item.PrivacyEntities = total.Entities
	return item, nil
}

func (s *privacyStats) add(other privacyStats) {
	s.Texts += other.Texts
	s.HitTexts += other.HitTexts
	s.Entities += other.Entities
}

func (p *privacyClient) redactBatch(ctx context.Context, texts []string) ([]privacyBatchResponse, error) {
	cleanTexts := make([]string, len(texts))
	for index, text := range texts {
		cleanTexts[index] = sanitizePostgresText(text)
	}
	payload, err := json.Marshal(map[string]any{"texts": cleanTexts})
	if err != nil {
		return nil, err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(p.baseURL.String(), "/")+"/redact/batch", bytes.NewReader(payload))
	if err != nil {
		return nil, err
	}
	request.Header.Set("Content-Type", "application/json")
	response, err := p.client.Do(request)
	if err != nil {
		return nil, fmt.Errorf("privacy filter request failed: %w", err)
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode != http.StatusOK {
		message, _ := io.ReadAll(io.LimitReader(response.Body, 4096))
		return nil, fmt.Errorf("privacy filter status %d: %s", response.StatusCode, strings.TrimSpace(string(message)))
	}
	var results []privacyBatchResponse
	if err := json.NewDecoder(io.LimitReader(response.Body, 64<<20)).Decode(&results); err != nil {
		return nil, fmt.Errorf("decode privacy filter response: %w", err)
	}
	for index := range results {
		results[index].Redacted = sanitizePostgresText(results[index].Redacted)
	}
	return results, nil
}

func sanitizePostgresText(value string) string {
	if !strings.ContainsRune(value, '\x00') {
		return value
	}
	return strings.ReplaceAll(value, "\x00", "\uFFFD")
}

func sanitizePostgresJSONStrings(document any) bool {
	changed := false
	switch value := document.(type) {
	case map[string]any:
		keys := make([]string, 0, len(value))
		for key := range value {
			keys = append(keys, key)
		}
		for _, key := range keys {
			child, exists := value[key]
			if !exists {
				continue
			}
			targetKey := key
			cleanKey := sanitizePostgresText(key)
			if cleanKey != key {
				delete(value, key)
				targetKey = cleanKey
				if _, collision := value[targetKey]; collision {
					base := targetKey + "#nul"
					targetKey = base
					for suffix := 2; ; suffix++ {
						if _, collision = value[targetKey]; !collision {
							break
						}
						targetKey = fmt.Sprintf("%s-%d", base, suffix)
					}
				}
				value[targetKey] = child
				changed = true
			}
			if text, ok := child.(string); ok {
				cleanText := sanitizePostgresText(text)
				if cleanText != text {
					value[targetKey] = cleanText
					changed = true
				}
				continue
			}
			if sanitizePostgresJSONStrings(child) {
				changed = true
			}
		}
	case []any:
		for index, child := range value {
			if text, ok := child.(string); ok {
				cleanText := sanitizePostgresText(text)
				if cleanText != text {
					value[index] = cleanText
					changed = true
				}
				continue
			}
			if sanitizePostgresJSONStrings(child) {
				changed = true
			}
		}
	}
	return changed
}

type privacyTarget struct {
	value string
	set   func(string)
}

func collectPrivacyTargets(document any) []privacyTarget {
	targets := make([]privacyTarget, 0, 16)
	var walk func(any, bool)
	walk = func(node any, active bool) {
		switch value := node.(type) {
		case map[string]any:
			for mapKey, child := range value {
				key := mapKey
				if isPrivacyExcludedKey(key) {
					continue
				}
				childActive := active || isPrivacyTextKey(key)
				if text, ok := child.(string); ok {
					if childActive {
						targets = append(targets, privacyTarget{
							value: text,
							set:   func(redacted string) { value[key] = redacted },
						})
					}
					continue
				}
				walk(child, childActive)
			}
		case []any:
			for sliceIndex := range value {
				index := sliceIndex
				if text, ok := value[index].(string); ok {
					if active {
						targets = append(targets, privacyTarget{
							value: text,
							set:   func(redacted string) { value[index] = redacted },
						})
					}
					continue
				}
				walk(value[index], active)
			}
		}
	}
	walk(document, false)
	return targets
}

func isPrivacyTextKey(key string) bool {
	switch strings.ToLower(key) {
	case "messages", "message", "content", "contents", "text", "parts",
		"input", "input_text", "output_text", "assistant_text", "final_text", "instructions", "system",
		"system_instruction", "prompt", "arguments", "tool_result",
		"function_response", "description":
		return true
	default:
		return false
	}
}

func isPrivacyExcludedKey(key string) bool {
	switch strings.ToLower(key) {
	case "model", "role", "type", "name", "id", "call_id", "tool_call_id",
		"status", "object", "format", "finish_reason", "reasoning_effort",
		"service_tier", "mime_type", "media_type", "encoding", "voice",
		"file_id", "image_url", "url", "data", "b64_json", "detail":
		return true
	default:
		return false
	}
}
