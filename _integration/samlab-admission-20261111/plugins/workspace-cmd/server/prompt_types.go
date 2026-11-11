package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"sort"
	"strings"
	"time"
)

const (
	maxPromptPolishRunes      = 65536
	maxPromptContextRunes     = 24000
	maxPromptContextMessages  = 12
	promptEmbeddingDimensions = 1024
)

var allowedPolishModes = map[string]struct{}{
	"conservative": {},
	"structured":   {},
	"concise":      {},
	"professional": {},
}

type promptContextMessage struct {
	ID      string `json:"id,omitempty"`
	Role    string `json:"role"`
	Content string `json:"content"`
}

type promptPolishRequest struct {
	Prompt                  string                 `json:"prompt"`
	Context                 []promptContextMessage `json:"context"`
	Mode                    string                 `json:"mode"`
	UseMemory               *bool                  `json:"use_memory,omitempty"`
	RAGSources              []string               `json:"rag_sources,omitempty"`
	PreviousOptimizedPrompt string                 `json:"previous_optimized_prompt,omitempty"`
	IterationFeedback       string                 `json:"iteration_feedback,omitempty"`
}

func validatePromptPolishRequest(request *promptPolishRequest) error {
	request.Prompt = strings.TrimSpace(request.Prompt)
	if request.Prompt == "" {
		return errors.New("prompt is required")
	}
	if len([]rune(request.Prompt)) > maxPromptPolishRunes {
		return fmt.Errorf("prompt exceeds %d characters", maxPromptPolishRunes)
	}
	request.Mode = strings.ToLower(strings.TrimSpace(request.Mode))
	if request.Mode == "" {
		request.Mode = "conservative"
	}
	if _, ok := allowedPolishModes[request.Mode]; !ok {
		return fmt.Errorf("unsupported polish mode %q", request.Mode)
	}
	if len(request.RAGSources) > 2 {
		return errors.New("rag_sources exceeds two knowledge types")
	}
	seenRAGSources := map[string]bool{}
	normalizedRAGSources := make([]string, 0, len(request.RAGSources))
	for _, source := range request.RAGSources {
		source = strings.ToLower(strings.TrimSpace(source))
		if !allowedKnowledgeTypes[source] {
			return fmt.Errorf("unsupported RAG source %q", source)
		}
		if !seenRAGSources[source] {
			seenRAGSources[source] = true
			normalizedRAGSources = append(normalizedRAGSources, source)
		}
	}
	request.RAGSources = normalizedRAGSources
	if len(request.Context) > maxPromptContextMessages {
		return fmt.Errorf("context exceeds %d messages", maxPromptContextMessages)
	}
	totalContextRunes := 0
	contextIDs := make(map[string]struct{}, len(request.Context))
	for index := range request.Context {
		message := &request.Context[index]
		message.ID = strings.TrimSpace(message.ID)
		if message.ID == "" {
			message.ID = fmt.Sprintf("ctx-%d", index+1)
		}
		if !validPromptContextID(message.ID) {
			return fmt.Errorf("invalid context id %q", message.ID)
		}
		if _, exists := contextIDs[message.ID]; exists {
			return fmt.Errorf("duplicate context id %q", message.ID)
		}
		contextIDs[message.ID] = struct{}{}
		message.Role = strings.ToLower(strings.TrimSpace(message.Role))
		if message.Role != "user" && message.Role != "assistant" {
			return fmt.Errorf("unsupported context role %q", message.Role)
		}
		message.Content = strings.TrimSpace(message.Content)
		if message.Content == "" {
			return fmt.Errorf("context message %q is empty", message.ID)
		}
		totalContextRunes += len([]rune(message.Content))
	}
	if totalContextRunes > maxPromptContextRunes {
		return fmt.Errorf("context exceeds %d characters", maxPromptContextRunes)
	}
	request.PreviousOptimizedPrompt = truncateExactRunes(request.PreviousOptimizedPrompt, maxPromptPolishRunes)
	request.IterationFeedback = truncateExactRunes(request.IterationFeedback, 4000)
	return nil
}

func validPromptContextID(value string) bool {
	if value == "" || len(value) > 64 {
		return false
	}
	for _, r := range value {
		if r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-' || r == '_' || r == '.' || r == ':' {
			continue
		}
		return false
	}
	return true
}

var allowedMemoryTypes = map[string]struct{}{
	"preference":          {},
	"project_context":     {},
	"glossary":            {},
	"successful_pattern":  {},
	"negative_preference": {},
}

type memoryCandidate struct {
	SourceKind string   `json:"source_kind"`
	SourceID   int64    `json:"source_id"`
	Type       string   `json:"type"`
	Title      string   `json:"title"`
	Content    string   `json:"content"`
	Keywords   []string `json:"keywords"`
	Confidence float64  `json:"confidence"`
	Importance int      `json:"importance"`
	Explicit   bool     `json:"explicit"`
	Conflict   bool     `json:"conflict"`
	Reason     string   `json:"reason"`
}

type memorySourceKey struct {
	Kind string
	ID   int64
}

type memorySource struct {
	IngestionID int64     `json:"-"`
	Kind        string    `json:"kind"`
	ID          int64     `json:"id"`
	UserID      int64     `json:"-"`
	Title       string    `json:"title"`
	UserText    string    `json:"user_text"`
	FinalText   string    `json:"final_text"`
	CompletedAt time.Time `json:"completed_at"`
	Hash        string    `json:"-"`
}

func normalizeMemoryCandidate(item memoryCandidate) (memoryCandidate, error) {
	item.Type = strings.ToLower(strings.TrimSpace(item.Type))
	if _, ok := allowedMemoryTypes[item.Type]; !ok {
		return memoryCandidate{}, fmt.Errorf("unsupported memory type %q", item.Type)
	}
	item.Title = truncateExactRunes(item.Title, 120)
	item.Content = truncateExactRunes(item.Content, 2000)
	item.Reason = truncateExactRunes(item.Reason, 500)
	if item.Content == "" {
		return memoryCandidate{}, errors.New("memory content is required")
	}
	if item.Confidence < 0 {
		item.Confidence = 0
	}
	if item.Confidence > 1 {
		item.Confidence = 1
	}
	if item.Importance < 1 {
		item.Importance = 1
	}
	if item.Importance > 5 {
		item.Importance = 5
	}
	seen := make(map[string]struct{})
	keywords := make([]string, 0, 12)
	for _, keyword := range item.Keywords {
		keyword = truncateExactRunes(keyword, 64)
		if keyword == "" {
			continue
		}
		key := strings.ToLower(keyword)
		if _, exists := seen[key]; exists {
			continue
		}
		seen[key] = struct{}{}
		keywords = append(keywords, keyword)
		if len(keywords) == 12 {
			break
		}
	}
	item.Keywords = keywords
	return item, nil
}

func decodeMemoryExtraction(content string, allowedSources map[memorySourceKey]memorySource) ([]memoryCandidate, error) {
	raw, err := decodeJSONObject(content)
	if err != nil {
		return nil, err
	}
	var payload struct {
		Candidates []memoryCandidate `json:"candidates"`
	}
	if err := decodeStrictJSON(raw, &payload); err != nil {
		return nil, err
	}
	if len(payload.Candidates) > 40 {
		return nil, errors.New("memory extraction returned too many candidates")
	}
	result := make([]memoryCandidate, 0, len(payload.Candidates))
	for _, candidate := range payload.Candidates {
		candidate.SourceKind = strings.ToLower(strings.TrimSpace(candidate.SourceKind))
		if candidate.SourceKind != "conversation" && candidate.SourceKind != "trajectory" || candidate.SourceID <= 0 {
			return nil, fmt.Errorf("invalid memory source %q/%d", candidate.SourceKind, candidate.SourceID)
		}
		candidate, err = normalizeMemoryCandidate(candidate)
		if err != nil {
			return nil, err
		}
		if _, ok := allowedSources[memorySourceKey{Kind: candidate.SourceKind, ID: candidate.SourceID}]; !ok {
			return nil, fmt.Errorf("memory candidate references source outside the batch: %s/%d", candidate.SourceKind, candidate.SourceID)
		}
		result = append(result, candidate)
	}
	return result, nil
}

func truncateExactRunes(value string, limit int) string {
	value = strings.TrimSpace(value)
	runes := []rune(value)
	if len(runes) <= limit {
		return value
	}
	return string(runes[:limit])
}

type polishModelResponse struct {
	OptimizedPrompt       string                `json:"optimized_prompt"`
	Changes               polishStringList      `json:"changes"`
	ClarifyingQuestions   polishStringList      `json:"clarifying_questions"`
	UsedMemoryIDs         []int64               `json:"used_memory_ids"`
	UsedContextMessageIDs polishStringList      `json:"used_context_message_ids"`
	ResolvedReferences    polishStringList      `json:"resolved_references"`
	AppliedConstraints    polishStringList      `json:"applied_constraints"`
	ModeApplied           string                `json:"mode_applied"`
	ModeContractVersion   polishContractVersion `json:"mode_contract_version"`
	Confidence            float64               `json:"confidence"`
	IgnoredMemoryIDs      int                   `json:"-"`
	IgnoredContextIDs     int                   `json:"-"`
	PromptTokens          int                   `json:"-"`
	CompletionTokens      int                   `json:"-"`
}

type polishStringList []string

func (values *polishStringList) UnmarshalJSON(data []byte) error {
	raw := strings.TrimSpace(string(data))
	if strings.HasPrefix(raw, "[") {
		var items []string
		if err := json.Unmarshal(data, &items); err != nil {
			return err
		}
		*values = polishStringList(items)
		return nil
	}
	var item string
	if err := json.Unmarshal(data, &item); err != nil {
		return errors.New("expected a string or an array of strings")
	}
	*values = polishStringList{item}
	return nil
}

type polishContractVersion string

func (value *polishContractVersion) UnmarshalJSON(data []byte) error {
	raw := strings.TrimSpace(string(data))
	if strings.HasPrefix(raw, `"`) {
		var text string
		if err := json.Unmarshal(data, &text); err != nil {
			return err
		}
		*value = polishContractVersion(strings.TrimSpace(text))
		return nil
	}
	if raw == promptModeContractVersion {
		*value = polishContractVersion(raw)
		return nil
	}
	return fmt.Errorf("mode_contract_version must be %q", promptModeContractVersion)
}

func decodePolishModelResponse(content string, allowedMemoryIDs map[int64]struct{}, allowedContextIDs map[string]struct{}, requestedMode string) (polishModelResponse, error) {
	raw, err := decodeJSONObject(content)
	if err != nil {
		return polishModelResponse{}, err
	}
	var result polishModelResponse
	if err := decodeStrictJSON(raw, &result); err != nil {
		return polishModelResponse{}, err
	}
	result.OptimizedPrompt = truncateRunes(result.OptimizedPrompt, 65536)
	if result.OptimizedPrompt == "" {
		return polishModelResponse{}, errors.New("optimized_prompt is required")
	}
	result.Changes = polishStringList(normalizeStringList([]string(result.Changes), 8, 240))
	result.ClarifyingQuestions = polishStringList(normalizeStringList([]string(result.ClarifyingQuestions), 5, 300))
	result.ResolvedReferences = polishStringList(normalizeStringList([]string(result.ResolvedReferences), 8, 300))
	result.AppliedConstraints = polishStringList(normalizeStringList([]string(result.AppliedConstraints), 8, 300))
	result.ModeApplied = strings.ToLower(strings.TrimSpace(result.ModeApplied))
	result.ModeContractVersion = polishContractVersion(strings.TrimSpace(string(result.ModeContractVersion)))
	if requestedMode != "" && result.ModeApplied != requestedMode {
		return polishModelResponse{}, fmt.Errorf("mode_applied must equal %q", requestedMode)
	}
	if requestedMode != "" && result.ModeContractVersion != promptModeContractVersion {
		return polishModelResponse{}, fmt.Errorf("mode_contract_version must equal %q", promptModeContractVersion)
	}
	if result.Confidence < 0 {
		result.Confidence = 0
	}
	if result.Confidence > 1 {
		result.Confidence = 1
	}
	seen := make(map[int64]struct{})
	memoryIDs := make([]int64, 0, len(result.UsedMemoryIDs))
	for _, id := range result.UsedMemoryIDs {
		if _, allowed := allowedMemoryIDs[id]; !allowed {
			result.IgnoredMemoryIDs++
			continue
		}
		if _, exists := seen[id]; exists {
			continue
		}
		seen[id] = struct{}{}
		memoryIDs = append(memoryIDs, id)
	}
	result.UsedMemoryIDs = memoryIDs
	seenContext := make(map[string]struct{})
	contextIDs := make([]string, 0, len(result.UsedContextMessageIDs))
	for _, id := range result.UsedContextMessageIDs {
		id = strings.TrimSpace(id)
		if _, allowed := allowedContextIDs[id]; !allowed {
			result.IgnoredContextIDs++
			continue
		}
		if _, exists := seenContext[id]; exists {
			continue
		}
		seenContext[id] = struct{}{}
		contextIDs = append(contextIDs, id)
	}
	result.UsedContextMessageIDs = polishStringList(contextIDs)
	if result.Changes == nil {
		result.Changes = polishStringList{}
	}
	if result.ClarifyingQuestions == nil {
		result.ClarifyingQuestions = polishStringList{}
	}
	if result.UsedMemoryIDs == nil {
		result.UsedMemoryIDs = []int64{}
	}
	if result.UsedContextMessageIDs == nil {
		result.UsedContextMessageIDs = polishStringList{}
	}
	if result.ResolvedReferences == nil {
		result.ResolvedReferences = polishStringList{}
	}
	if result.AppliedConstraints == nil {
		result.AppliedConstraints = polishStringList{}
	}
	return result, nil
}

func decodeStrictJSON(raw []byte, target any) error {
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("multiple JSON values are not allowed")
		}
		return err
	}
	return nil
}

func normalizeStringList(values []string, maxItems, maxRunes int) []string {
	result := make([]string, 0, maxItems)
	for _, value := range values {
		value = truncateRunes(value, maxRunes)
		if value == "" {
			continue
		}
		result = append(result, value)
		if len(result) == maxItems {
			break
		}
	}
	return result
}

func decodeEmbeddingVector(raw []byte) ([]float64, error) {
	var payload struct {
		Data []struct {
			Embedding []float64 `json:"embedding"`
		} `json:"data"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, err
	}
	if len(payload.Data) != 1 || len(payload.Data[0].Embedding) != promptEmbeddingDimensions {
		return nil, fmt.Errorf("embedding must contain exactly %d dimensions", promptEmbeddingDimensions)
	}
	for _, value := range payload.Data[0].Embedding {
		if math.IsNaN(value) || math.IsInf(value, 0) {
			return nil, errors.New("embedding contains a non-finite value")
		}
	}
	return payload.Data[0].Embedding, nil
}

type weightedMemoryRankList struct {
	Items  []retrievedMemory
	Weight float64
}

func fuseMemoryRanks(semantic, lexical, recent []retrievedMemory, now time.Time, limit int) []retrievedMemory {
	return fuseWeightedMemoryRanks([]weightedMemoryRankList{
		{Items: semantic, Weight: 1},
		{Items: lexical, Weight: 1},
		{Items: recent, Weight: 1},
	}, now, limit)
}

func fuseWeightedMemoryRanks(lists []weightedMemoryRankList, now time.Time, limit int) []retrievedMemory {
	type scoredMemory struct {
		item  retrievedMemory
		score float64
	}
	items := make(map[int64]*scoredMemory)
	add := func(values []retrievedMemory, weight float64) {
		if weight <= 0 {
			return
		}
		for index, item := range values {
			entry := items[item.ID]
			if entry == nil {
				copy := item
				entry = &scoredMemory{item: copy}
				items[item.ID] = entry
			}
			entry.score += weight / (60 + float64(index+1))
		}
	}
	for _, list := range lists {
		add(list.Items, list.Weight)
	}
	result := make([]retrievedMemory, 0, len(items))
	for _, entry := range items {
		entry.score += entry.item.Confidence*0.005 + float64(entry.item.Importance)*0.001
		if entry.item.Pinned {
			entry.score += 0.02
		}
		age := now.Sub(entry.item.LastSeenAt)
		if age < 0 {
			age = 0
		}
		if age < 90*24*time.Hour {
			entry.score += 0.005 * (1 - age.Hours()/(90*24))
		}
		entry.item.Score = entry.score
		result = append(result, entry.item)
	}
	sort.Slice(result, func(i, j int) bool {
		if result[i].Score == result[j].Score {
			return result[i].ID < result[j].ID
		}
		return result[i].Score > result[j].Score
	})
	if limit > 0 && len(result) > limit {
		result = result[:limit]
	}
	return result
}
