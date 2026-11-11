package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

type promptMemorySettingsValue struct {
	GenerateMemories bool      `json:"generate_memories"`
	UseMemories      bool      `json:"use_memories"`
	RetentionDays    int       `json:"retention_days"`
	IngestNotBefore  time.Time `json:"ingest_not_before"`
	MaxActive        int       `json:"max_active_memories"`
	MaxPinned        int       `json:"max_pinned_memories"`
}

type promptMemoryItem struct {
	ID            int64      `json:"id"`
	Type          string     `json:"type"`
	Title         string     `json:"title"`
	Content       string     `json:"content"`
	Keywords      []string   `json:"keywords"`
	Confidence    float64    `json:"confidence"`
	Importance    int        `json:"importance"`
	Explicit      bool       `json:"explicit"`
	Pinned        bool       `json:"pinned"`
	Origin        string     `json:"origin"`
	Status        string     `json:"status"`
	EvidenceCount int        `json:"evidence_count"`
	FirstSeenAt   time.Time  `json:"first_seen_at"`
	LastSeenAt    time.Time  `json:"last_seen_at"`
	ExpiresAt     *time.Time `json:"expires_at,omitempty"`
	CreatedAt     time.Time  `json:"created_at"`
	UpdatedAt     time.Time  `json:"updated_at"`
}

func (a *app) ensurePromptMemorySettings(ctx context.Context, userID int64) (promptMemorySettingsValue, error) {
	_, err := a.db.Exec(ctx, `INSERT INTO prompt_memory_settings(user_id) VALUES($1) ON CONFLICT(user_id) DO NOTHING`, userID)
	if err != nil {
		return promptMemorySettingsValue{}, err
	}
	var item promptMemorySettingsValue
	err = a.db.QueryRow(ctx, `SELECT generate_memories,use_memories,retention_days,ingest_not_before,max_active_memories,max_pinned_memories
		FROM prompt_memory_settings WHERE user_id=$1`, userID).Scan(
		&item.GenerateMemories, &item.UseMemories, &item.RetentionDays, &item.IngestNotBefore, &item.MaxActive, &item.MaxPinned)
	return item, err
}

func (a *app) promptMemorySettings(w http.ResponseWriter, r *http.Request) {
	id, ok := a.requireAuth(w, r)
	if !ok {
		return
	}
	switch r.Method {
	case http.MethodGet:
		settings, err := a.ensurePromptMemorySettings(r.Context(), id.ID)
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, "failed to load prompt memory settings")
			return
		}
		writeJSON(w, http.StatusOK, settings)
	case http.MethodPut:
		var body struct {
			GenerateMemories *bool `json:"generate_memories"`
			UseMemories      *bool `json:"use_memories"`
		}
		if err := readJSON(r, &body, 1<<20); err != nil || (body.GenerateMemories == nil && body.UseMemories == nil) {
			writeAPIError(w, http.StatusBadRequest, "invalid prompt memory settings")
			return
		}
		current, err := a.ensurePromptMemorySettings(r.Context(), id.ID)
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, "failed to load prompt memory settings")
			return
		}
		wasGenerating := current.GenerateMemories
		if body.GenerateMemories != nil {
			current.GenerateMemories = *body.GenerateMemories
		}
		if body.UseMemories != nil {
			current.UseMemories = *body.UseMemories
		}
		_, err = a.db.Exec(r.Context(), `UPDATE prompt_memory_settings
			SET generate_memories=$2,use_memories=$3,
			ingest_not_before=CASE WHEN $4 THEN GREATEST(ingest_not_before,now()-INTERVAL '30 days') ELSE ingest_not_before END,
			updated_at=now() WHERE user_id=$1`, id.ID, current.GenerateMemories, current.UseMemories, !wasGenerating && current.GenerateMemories)
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, "failed to update prompt memory settings")
			return
		}
		if current.GenerateMemories && a.memoryWake != nil {
			select {
			case a.memoryWake <- struct{}{}:
			default:
			}
		}
		updated, _ := a.ensurePromptMemorySettings(r.Context(), id.ID)
		writeJSON(w, http.StatusOK, updated)
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func (a *app) promptMemories(w http.ResponseWriter, r *http.Request) {
	id, ok := a.requireAuth(w, r)
	if !ok {
		return
	}
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	page := positiveQueryInt(r, "page", 1, 1, 100000)
	pageSize := positiveQueryInt(r, "page_size", 30, 1, 100)
	memoryType := strings.TrimSpace(r.URL.Query().Get("type"))
	query := truncateExactRunes(r.URL.Query().Get("q"), 500)
	args := []any{id.ID}
	where := []string{"user_id=$1", "status='active'", "(pinned OR expires_at > now())"}
	if memoryType != "" {
		if _, valid := allowedMemoryTypes[memoryType]; !valid {
			writeAPIError(w, http.StatusBadRequest, "invalid memory type")
			return
		}
		args = append(args, memoryType)
		where = append(where, fmt.Sprintf("memory_type=$%d", len(args)))
	}
	if query != "" {
		args = append(args, query)
		where = append(where, fmt.Sprintf("(search_text %% $%d OR search_text ILIKE '%%' || $%d || '%%')", len(args), len(args)))
	}
	whereSQL := strings.Join(where, " AND ")
	var total int64
	if err := a.db.QueryRow(r.Context(), "SELECT COUNT(*) FROM prompt_memories WHERE "+whereSQL, args...).Scan(&total); err != nil {
		writeAPIError(w, http.StatusInternalServerError, "failed to count prompt memories")
		return
	}
	args = append(args, pageSize, (page-1)*pageSize)
	rows, err := a.db.Query(r.Context(), `SELECT id,memory_type,title,content,keywords,confidence,importance,explicit,pinned,origin,status,
		evidence_count,first_seen_at,last_seen_at,expires_at,created_at,updated_at FROM prompt_memories WHERE `+whereSQL+
		fmt.Sprintf(" ORDER BY pinned DESC,importance DESC,last_seen_at DESC,id DESC LIMIT $%d OFFSET $%d", len(args)-1, len(args)), args...)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "failed to list prompt memories")
		return
	}
	defer rows.Close()
	items := make([]promptMemoryItem, 0, pageSize)
	for rows.Next() {
		item, scanErr := scanPromptMemory(rows)
		if scanErr != nil {
			writeAPIError(w, http.StatusInternalServerError, "failed to scan prompt memories")
			return
		}
		items = append(items, item)
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items, "total": total, "page": page, "page_size": pageSize})
}

type memoryRowScanner interface{ Scan(...any) error }

func scanPromptMemory(row memoryRowScanner) (promptMemoryItem, error) {
	var item promptMemoryItem
	err := row.Scan(&item.ID, &item.Type, &item.Title, &item.Content, &item.Keywords, &item.Confidence, &item.Importance,
		&item.Explicit, &item.Pinned, &item.Origin, &item.Status, &item.EvidenceCount, &item.FirstSeenAt, &item.LastSeenAt,
		&item.ExpiresAt, &item.CreatedAt, &item.UpdatedAt)
	return item, err
}

func (a *app) promptMemoryByID(w http.ResponseWriter, r *http.Request) {
	id, ok := a.requireAuth(w, r)
	if !ok {
		return
	}
	raw := strings.Trim(strings.TrimPrefix(r.URL.Path, "/_workspace/api/v1/prompt-memories/"), "/")
	if raw == "all" && r.Method == http.MethodDelete {
		tx, err := a.db.Begin(r.Context())
		if err == nil {
			_, err = tx.Exec(r.Context(), `UPDATE prompt_memories SET status='deleted',pinned=false,updated_at=now() WHERE user_id=$1 AND status <> 'deleted'`, id.ID)
		}
		if err == nil {
			_, err = tx.Exec(r.Context(), `INSERT INTO prompt_memory_settings(user_id,ingest_not_before) VALUES($1,now())
				ON CONFLICT(user_id) DO UPDATE SET ingest_not_before=now(),updated_at=now()`, id.ID)
		}
		if err == nil {
			_, err = tx.Exec(r.Context(), `UPDATE prompt_memory_ingestions SET status='skipped',last_error='deleted by user',
				processed_at=now(),updated_at=now() WHERE user_id=$1 AND status IN ('queued','failed','processing')`, id.ID)
		}
		if err != nil {
			if tx != nil {
				_ = tx.Rollback(r.Context())
			}
			writeAPIError(w, http.StatusInternalServerError, "failed to delete prompt memories")
			return
		}
		if err := tx.Commit(r.Context()); err != nil {
			writeAPIError(w, http.StatusInternalServerError, "failed to delete prompt memories")
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"deleted": true})
		return
	}
	memoryID, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || memoryID <= 0 {
		writeAPIError(w, http.StatusBadRequest, "invalid prompt memory id")
		return
	}
	if r.Method == http.MethodDelete {
		result, err := a.db.Exec(r.Context(), `UPDATE prompt_memories SET status='deleted',pinned=false,updated_at=now() WHERE id=$1 AND user_id=$2 AND status <> 'deleted'`, memoryID, id.ID)
		if err != nil || result.RowsAffected() == 0 {
			writeAPIError(w, http.StatusNotFound, "prompt memory not found")
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"deleted": true})
		return
	}
	if r.Method != http.MethodPatch {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		Title    *string   `json:"title"`
		Content  *string   `json:"content"`
		Keywords *[]string `json:"keywords"`
		Pinned   *bool     `json:"pinned"`
	}
	if err := readJSON(r, &body, 1<<20); err != nil {
		writeAPIError(w, http.StatusBadRequest, "invalid prompt memory update")
		return
	}
	item, err := scanPromptMemory(a.db.QueryRow(r.Context(), `SELECT id,memory_type,title,content,keywords,confidence,importance,explicit,pinned,origin,status,
		evidence_count,first_seen_at,last_seen_at,expires_at,created_at,updated_at FROM prompt_memories WHERE id=$1 AND user_id=$2 AND status='active'`, memoryID, id.ID))
	if err != nil {
		writeAPIError(w, http.StatusNotFound, "prompt memory not found")
		return
	}
	if body.Title != nil {
		item.Title = truncateExactRunes(*body.Title, 120)
	}
	if body.Content != nil {
		item.Content = truncateExactRunes(*body.Content, 2000)
	}
	if body.Keywords != nil {
		item.Keywords = normalizeMemoryKeywords(*body.Keywords)
	}
	if body.Pinned != nil {
		item.Pinned = *body.Pinned
	}
	if item.Content == "" {
		writeAPIError(w, http.StatusBadRequest, "prompt memory content is required")
		return
	}
	settings, _ := a.ensurePromptMemorySettings(r.Context(), id.ID)
	if item.Pinned {
		var pinned int
		_ = a.db.QueryRow(r.Context(), `SELECT COUNT(*) FROM prompt_memories WHERE user_id=$1 AND status='active' AND pinned AND id<>$2`, id.ID, memoryID).Scan(&pinned)
		if pinned >= settings.MaxPinned {
			writeAPIError(w, http.StatusConflict, "pinned prompt memory limit reached")
			return
		}
	}
	redacted, _, err := a.redactMemoryTexts(r.Context(), []string{item.Title, item.Content, strings.Join(item.Keywords, "\n")})
	if err != nil || len(redacted) != 3 {
		writeAPIError(w, http.StatusServiceUnavailable, "privacy filter unavailable")
		return
	}
	item.Title, item.Content = redacted[0], redacted[1]
	item.Keywords = normalizeMemoryKeywords(strings.Split(redacted[2], "\n"))
	client := a.promptClient()
	vector, err := client.embed(r.Context(), memorySearchText(item.Title, item.Content, item.Keywords))
	if err != nil {
		writeAPIError(w, http.StatusServiceUnavailable, "embedding model unavailable")
		return
	}
	expiresAt := any(time.Now().UTC().Add(time.Duration(settings.RetentionDays) * 24 * time.Hour))
	if item.Pinned {
		expiresAt = nil
	}
	_, err = a.db.Exec(r.Context(), `UPDATE prompt_memories SET title=$3,content=$4,keywords=$5,search_text=$6,embedding=$7::vector,
		embedding_model=$8,pinned=$9,origin='manual',expires_at=$10,updated_at=now() WHERE id=$1 AND user_id=$2`,
		memoryID, id.ID, item.Title, item.Content, item.Keywords, memorySearchText(item.Title, item.Content, item.Keywords), vectorLiteral(vector),
		a.cfg.PromptEmbeddingModel, item.Pinned, expiresAt)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "failed to update prompt memory")
		return
	}
	updated, _ := scanPromptMemory(a.db.QueryRow(r.Context(), `SELECT id,memory_type,title,content,keywords,confidence,importance,explicit,pinned,origin,status,
		evidence_count,first_seen_at,last_seen_at,expires_at,created_at,updated_at FROM prompt_memories WHERE id=$1 AND user_id=$2`, memoryID, id.ID))
	writeJSON(w, http.StatusOK, updated)
}

func (a *app) promptPolish(w http.ResponseWriter, r *http.Request) {
	id, ok := a.requireAuth(w, r)
	if !ok {
		return
	}
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var body promptPolishRequest
	if err := readJSON(r, &body, 1<<20); err != nil {
		writeAPIError(w, http.StatusBadRequest, "invalid prompt polish request")
		return
	}
	if err := validatePromptPolishRequest(&body); err != nil {
		writeAPIError(w, http.StatusBadRequest, "invalid prompt polish request: "+err.Error())
		return
	}
	started := time.Now()
	runID := randomUUID()
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	settings, err := a.ensurePromptMemorySettings(ctx, id.ID)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "failed to load prompt memory settings")
		return
	}
	useMemory := settings.UseMemories
	if body.UseMemory != nil {
		useMemory = useMemory && *body.UseMemory
	}
	ragSettings, err := a.ensurePromptRAGSettings(ctx, id.ID)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "failed to load knowledge settings")
		return
	}
	warnings := make([]string, 0, 4)
	personalMemories := []retrievedMemory{}
	client := a.promptClient()
	if useMemory {
		personalMemories, warnings = a.retrievePromptMemories(ctx, id.ID, body.Prompt, body.Context, client)
	}
	effectiveSources := effectiveRAGSources(ragSettings, body.RAGSources)
	knowledgeMemories, knowledgeBySyntheticID, knowledgeWarnings := a.retrieveKnowledgeMemories(ctx, id.ID, effectiveSources, body.Prompt, body.Context, client)
	warnings = append(warnings, knowledgeWarnings...)
	memories := append(append([]retrievedMemory{}, personalMemories...), knowledgeMemories...)
	result, err := client.polishWithGuard(ctx, body, memories)
	if err != nil {
		log.Printf("prompt polish failed user_id=%d run_id=%s mode=%s model=%s: %v", id.ID, runID, body.Mode, a.cfg.PromptPolishModel, err)
		a.recordPromptPolishRun(runID, id.ID, body, useMemory, nil, nil, nil, nil, false, "failed", []string{"model_unavailable"}, time.Since(started), 0, 0)
		writeAPIError(w, http.StatusServiceUnavailable, "prompt polish model unavailable")
		return
	}
	warnings = append(warnings, result.Warnings...)
	warnings = uniqueStrings(warnings)
	changed := strings.TrimSpace(result.OptimizedPrompt) != strings.TrimSpace(body.Prompt)
	status := "succeeded"
	if len(warnings) > 0 {
		status = "fallback"
	}
	usedMemoryIDs := make([]int64, 0, len(result.UsedMemoryIDs))
	usedKnowledgeIDs := make([]int64, 0, len(result.UsedMemoryIDs))
	ragCitations := make([]knowledgeCitation, 0, len(result.UsedMemoryIDs))
	for _, usedID := range result.UsedMemoryIDs {
		if citation, ok := knowledgeBySyntheticID[usedID]; ok {
			usedKnowledgeIDs = append(usedKnowledgeIDs, citation.ChunkID)
			ragCitations = append(ragCitations, citation)
			continue
		}
		usedMemoryIDs = append(usedMemoryIDs, usedID)
	}
	a.recordPromptPolishRun(runID, id.ID, body, useMemory, &result.OptimizedPrompt, usedMemoryIDs, usedKnowledgeIDs, result.UsedContextMessageIDs, changed, status, warnings, time.Since(started), result.PromptTokens, result.CompletionTokens)
	refs := make([]retrievedMemory, 0, len(usedMemoryIDs))
	for _, memoryID := range usedMemoryIDs {
		for _, memory := range personalMemories {
			if memory.ID == memoryID {
				refs = append(refs, memory)
				break
			}
		}
	}
	ignoredContextIDs := unusedPromptContextIDs(body.Context, result.UsedContextMessageIDs)
	writeJSON(w, http.StatusOK, map[string]any{
		"run_id": runID, "original_prompt": body.Prompt, "optimized_prompt": result.OptimizedPrompt,
		"changed": changed, "changes": result.Changes, "clarifying_questions": result.ClarifyingQuestions,
		"used_memories": refs, "rag_citations": ragCitations, "warnings": warnings, "confidence": result.Confidence,
		"mode_applied": result.ModeApplied, "mode_contract_version": result.ModeContractVersion,
		"context_usage": map[string]any{
			"provided_count": len(body.Context), "used_message_ids": result.UsedContextMessageIDs,
			"resolved_references": result.ResolvedReferences, "applied_constraints": result.AppliedConstraints,
			"ignored_message_ids": ignoredContextIDs,
		},
		"model": a.cfg.PromptPolishModel, "latency_ms": time.Since(started).Milliseconds(),
	})
}

func (a *app) recordPromptPolishRun(runID string, userID int64, request promptPolishRequest, useMemory bool, optimized *string, usedIDs, usedKnowledgeIDs []int64, usedContextIDs []string, changed bool, status string, warnings []string, latency time.Duration, promptTokens, completionTokens int) {
	var optimizedHash any
	outputChars := 0
	if optimized != nil {
		optimizedHash = hashText(*optimized)
		outputChars = len([]rune(*optimized))
	}
	var contextHash any
	if len(request.Context) > 0 {
		encoded, _ := json.Marshal(request.Context)
		contextHash = hashText(string(encoded))
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if usedIDs == nil {
		usedIDs = []int64{}
	}
	if usedKnowledgeIDs == nil {
		usedKnowledgeIDs = []int64{}
	}
	if warnings == nil {
		warnings = []string{}
	}
	_, err := a.db.Exec(ctx, `INSERT INTO prompt_polish_runs(id,user_id,prompt_sha256,optimized_sha256,mode,use_memory,used_memory_ids,used_knowledge_chunk_ids,
		changed,status,model,latency_ms,prompt_tokens,completion_tokens,warning_codes,mode_contract_version,input_chars,output_chars,
		context_message_count,used_context_count,context_sha256)
		VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21)`,
		runID, userID, hashText(request.Prompt), optimizedHash, request.Mode, useMemory, usedIDs, usedKnowledgeIDs, changed, status,
		a.cfg.PromptPolishModel, latency.Milliseconds(), nullPositiveInt(promptTokens), nullPositiveInt(completionTokens), warnings,
		promptModeContractVersion, len([]rune(request.Prompt)), outputChars, len(request.Context), len(usedContextIDs), contextHash)
	if err != nil {
		log.Printf("prompt polish run record failed user_id=%d run_id=%s: %v", userID, runID, err)
	}
}

func (a *app) promptPolishFeedback(w http.ResponseWriter, r *http.Request) {
	id, ok := a.requireAuth(w, r)
	if !ok {
		return
	}
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		RunID        string   `json:"run_id"`
		Action       string   `json:"action"`
		EditDistance *float64 `json:"edit_distance,omitempty"`
	}
	if err := readJSON(r, &body, 1<<20); err != nil || !looksLikeUUID(body.RunID) {
		writeAPIError(w, http.StatusBadRequest, "invalid prompt polish feedback")
		return
	}
	allowed := map[string]bool{"accepted": true, "copied": true, "rejected": true, "edited": true, "iterated": true}
	if !allowed[body.Action] || (body.EditDistance != nil && (*body.EditDistance < 0 || *body.EditDistance > 1)) {
		writeAPIError(w, http.StatusBadRequest, "invalid prompt polish feedback")
		return
	}
	result, err := a.db.Exec(r.Context(), `INSERT INTO prompt_polish_feedback(run_id,user_id,action,edit_distance)
		SELECT id,$2,$3,$4 FROM prompt_polish_runs WHERE id=$1 AND user_id=$2
		ON CONFLICT(run_id,user_id,action) DO UPDATE SET edit_distance=excluded.edit_distance`, body.RunID, id.ID, body.Action, body.EditDistance)
	if err != nil || result.RowsAffected() == 0 {
		writeAPIError(w, http.StatusNotFound, "prompt polish run not found")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"saved": true})
}

func (a *app) promptClient() promptModelClient {
	return promptModelClient{
		HTTP: a.http, BaseURL: a.cfg.PromptModelBaseURL, APIKey: a.cfg.PromptModelAPIKey, Model: a.cfg.PromptPolishModel,
		EmbeddingBaseURL: a.cfg.PromptEmbeddingBaseURL, EmbeddingAPIKey: a.cfg.PromptEmbeddingAPIKey,
		EmbeddingModel: a.cfg.PromptEmbeddingModel, DeepSeekAPI: isDeepSeekAPIURL(a.cfg.PromptModelBaseURL),
	}
}

func (a *app) retrievePromptMemories(ctx context.Context, userID int64, prompt string, contextMessages []promptContextMessage, client promptModelClient) ([]retrievedMemory, []string) {
	warnings := make([]string, 0, 3)
	promptQuery := truncateExactRunes(prompt, 12_000)
	contextQuery := buildContextRetrievalText(contextMessages, 6_000)
	type embeddingResult struct {
		name   string
		vector []float64
		err    error
	}
	queries := map[string]string{"prompt": promptQuery}
	if contextQuery != "" {
		queries["context"] = contextQuery
	}
	results := make(chan embeddingResult, len(queries))
	var wait sync.WaitGroup
	for name, query := range queries {
		wait.Add(1)
		go func(name, query string) {
			defer wait.Done()
			vector, err := client.embed(ctx, query)
			results <- embeddingResult{name: name, vector: vector, err: err}
		}(name, query)
	}
	wait.Wait()
	close(results)
	vectors := make(map[string][]float64, len(queries))
	for result := range results {
		if result.err != nil {
			if result.name == "prompt" {
				warnings = append(warnings, "embedding_fallback")
			} else {
				warnings = append(warnings, "context_embedding_fallback")
			}
			continue
		}
		vectors[result.name] = result.vector
	}
	rankLists := make([]weightedMemoryRankList, 0, 5)
	for _, item := range []struct {
		name   string
		query  string
		weight float64
	}{
		{name: "prompt", query: promptQuery, weight: 1},
		{name: "context", query: contextQuery, weight: 0.6},
	} {
		if vector := vectors[item.name]; len(vector) > 0 {
			semantic, err := a.queryRetrievedMemories(ctx, `SELECT id,memory_type,title,content,confidence,importance,pinned,last_seen_at
				FROM prompt_memories WHERE user_id=$1 AND status='active' AND (pinned OR expires_at>now()) AND embedding IS NOT NULL
				ORDER BY embedding <=> $2::vector LIMIT 20`, userID, vectorLiteral(vector))
			if err != nil {
				warnings = append(warnings, item.name+"_semantic_retrieval_failed")
			} else {
				rankLists = append(rankLists, weightedMemoryRankList{Items: semantic, Weight: item.weight})
			}
		}
		if item.query != "" {
			lexical, err := a.queryRetrievedMemories(ctx, `SELECT id,memory_type,title,content,confidence,importance,pinned,last_seen_at
				FROM prompt_memories WHERE user_id=$1 AND status='active' AND (pinned OR expires_at>now())
				ORDER BY similarity(search_text,$2) DESC,confidence DESC,last_seen_at DESC LIMIT 20`, userID, item.query)
			if err != nil {
				warnings = append(warnings, item.name+"_lexical_retrieval_failed")
			} else {
				rankLists = append(rankLists, weightedMemoryRankList{Items: lexical, Weight: item.weight})
			}
		}
	}
	recent, err := a.queryRetrievedMemories(ctx, `SELECT id,memory_type,title,content,confidence,importance,pinned,last_seen_at
		FROM prompt_memories WHERE user_id=$1 AND status='active' AND (pinned OR expires_at>now())
		ORDER BY pinned DESC,importance DESC,last_seen_at DESC LIMIT 10`, userID)
	if err != nil {
		warnings = append(warnings, "recent_retrieval_failed")
	} else {
		rankLists = append(rankLists, weightedMemoryRankList{Items: recent, Weight: 0.25})
	}
	items := fuseWeightedMemoryRanks(rankLists, time.Now().UTC(), 8)
	total := 0
	trimmed := make([]retrievedMemory, 0, len(items))
	for _, item := range items {
		size := len([]rune(item.Title)) + len([]rune(item.Content))
		if total+size > 6000 {
			continue
		}
		total += size
		trimmed = append(trimmed, item)
	}
	return trimmed, uniqueStrings(warnings)
}

func unusedPromptContextIDs(messages []promptContextMessage, usedIDs []string) []string {
	used := make(map[string]struct{}, len(usedIDs))
	for _, id := range usedIDs {
		used[id] = struct{}{}
	}
	ignored := make([]string, 0, len(messages))
	for _, message := range messages {
		if _, ok := used[message.ID]; !ok {
			ignored = append(ignored, message.ID)
		}
	}
	return ignored
}

func nullPositiveInt(value int) any {
	if value <= 0 {
		return nil
	}
	return value
}

func (a *app) queryRetrievedMemories(ctx context.Context, query string, args ...any) ([]retrievedMemory, error) {
	rows, err := a.db.Query(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := make([]retrievedMemory, 0, 20)
	for rows.Next() {
		var item retrievedMemory
		if err := rows.Scan(&item.ID, &item.Type, &item.Title, &item.Content, &item.Confidence, &item.Importance, &item.Pinned, &item.LastSeenAt); err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	return items, rows.Err()
}

func positiveQueryInt(r *http.Request, key string, fallback, minValue, maxValue int) int {
	value, err := strconv.Atoi(r.URL.Query().Get(key))
	if err != nil || value < minValue {
		return fallback
	}
	if value > maxValue {
		return maxValue
	}
	return value
}

func normalizeMemoryKeywords(values []string) []string {
	item, _ := normalizeMemoryCandidate(memoryCandidate{Type: "preference", Content: "placeholder", Keywords: values, Importance: 1})
	return item.Keywords
}

func memorySearchText(title, content string, keywords []string) string {
	return strings.TrimSpace(title + "\n" + content + "\n" + strings.Join(keywords, " "))
}

func vectorLiteral(values []float64) string {
	parts := make([]string, len(values))
	for index, value := range values {
		parts[index] = strconv.FormatFloat(value, 'g', -1, 64)
	}
	return "[" + strings.Join(parts, ",") + "]"
}

func hashText(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

func randomUUID() string {
	value := make([]byte, 16)
	if _, err := rand.Read(value); err != nil {
		panic(err)
	}
	value[6] = (value[6] & 0x0f) | 0x40
	value[8] = (value[8] & 0x3f) | 0x80
	hexValue := hex.EncodeToString(value)
	return hexValue[0:8] + "-" + hexValue[8:12] + "-" + hexValue[12:16] + "-" + hexValue[16:20] + "-" + hexValue[20:]
}

func looksLikeUUID(value string) bool {
	if len(value) != 36 || value[8] != '-' || value[13] != '-' || value[18] != '-' || value[23] != '-' {
		return false
	}
	_, err := hex.DecodeString(strings.ReplaceAll(value, "-", ""))
	return err == nil
}

func uniqueStrings(values []string) []string {
	seen := make(map[string]struct{}, len(values))
	result := make([]string, 0, len(values))
	for _, value := range values {
		if value == "" {
			continue
		}
		if _, exists := seen[value]; exists {
			continue
		}
		seen[value] = struct{}{}
		result = append(result, value)
	}
	return result
}
