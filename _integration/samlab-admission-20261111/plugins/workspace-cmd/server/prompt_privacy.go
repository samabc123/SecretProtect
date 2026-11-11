package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

type promptPrivacyResult struct {
	Redacted string `json:"redacted"`
	Hit      bool   `json:"hit"`
	Count    int    `json:"count"`
}

func (a *app) redactMemoryTexts(ctx context.Context, texts []string) ([]string, int, error) {
	if strings.TrimSpace(a.cfg.PrivacyFilterURL) == "" {
		return nil, 0, errors.New("privacy filter is not configured")
	}
	clean := make([]string, len(texts))
	for index, value := range texts {
		clean[index] = strings.ReplaceAll(value, "\x00", "")
	}
	body, _ := json.Marshal(map[string]any{"texts": clean})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(a.cfg.PrivacyFilterURL, "/")+"/redact/batch", bytes.NewReader(body))
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	client := a.http
	if client == nil {
		client = &http.Client{Timeout: 10 * time.Second}
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		data, _ := io.ReadAll(io.LimitReader(resp.Body, 2000))
		return nil, 0, fmt.Errorf("privacy filter status %d: %s", resp.StatusCode, truncateRunes(string(data), 300))
	}
	var results []promptPrivacyResult
	if err := json.NewDecoder(io.LimitReader(resp.Body, 8<<20)).Decode(&results); err != nil {
		return nil, 0, err
	}
	if len(results) != len(texts) {
		return nil, 0, fmt.Errorf("privacy filter returned %d results for %d texts", len(results), len(texts))
	}
	redacted := make([]string, len(results))
	entities := 0
	for index, result := range results {
		redacted[index] = strings.ReplaceAll(result.Redacted, "\x00", "")
		entities += result.Count
	}
	return redacted, entities, nil
}
