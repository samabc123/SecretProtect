package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

type promptContextSourceSummary struct {
	Kind        string    `json:"kind"`
	ID          int64     `json:"id"`
	Title       string    `json:"title"`
	Preview     string    `json:"preview"`
	Model       string    `json:"model"`
	CompletedAt time.Time `json:"completed_at"`
}

func (a *app) promptContextSources(w http.ResponseWriter, r *http.Request) {
	id, ok := a.requireAuth(w, r)
	if !ok {
		return
	}
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	kind := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("kind")))
	if kind != "" && kind != "conversation" && kind != "trajectory" {
		writeAPIError(w, http.StatusBadRequest, "invalid context source kind")
		return
	}
	query := truncateExactRunes(r.URL.Query().Get("q"), 200)
	page := positiveQueryInt(r, "page", 1, 1, 100000)
	pageSize := positiveQueryInt(r, "page_size", 20, 1, 50)
	rows, err := a.db.Query(r.Context(), `SELECT source_kind,source_id,title,preview,model,completed_at,COUNT(*) OVER()
		FROM (
			SELECT 'conversation'::text AS source_kind,id AS source_id,
				COALESCE(NULLIF(model,''),'Conversation')::text AS title,
				LEFT(COALESCE(assistant_text,''),240)::text AS preview,COALESCE(model,'')::text AS model,completed_at
			FROM conversation_archives WHERE user_id=$1 AND completed_at >= now()-INTERVAL '30 days'
			UNION ALL
			SELECT 'trajectory'::text,id,COALESCE(NULLIF(title,''),'Agent trajectory')::text,
				LEFT(COALESCE(final_text,''),240)::text,COALESCE(model,'')::text,completed_at
			FROM agent_trajectories WHERE user_id=$1 AND completed_at >= now()-INTERVAL '30 days'
		) sources
		WHERE ($2='' OR source_kind=$2) AND ($3='' OR title ILIKE '%'||$3||'%' OR preview ILIKE '%'||$3||'%')
		ORDER BY completed_at DESC,source_kind,source_id DESC LIMIT $4 OFFSET $5`, id.ID, kind, query, pageSize, (page-1)*pageSize)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, "failed to list prompt context sources")
		return
	}
	defer rows.Close()
	items := make([]promptContextSourceSummary, 0, pageSize)
	var total int64
	for rows.Next() {
		var item promptContextSourceSummary
		if err := rows.Scan(&item.Kind, &item.ID, &item.Title, &item.Preview, &item.Model, &item.CompletedAt, &total); err != nil {
			writeAPIError(w, http.StatusInternalServerError, "failed to scan prompt context sources")
			return
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		writeAPIError(w, http.StatusInternalServerError, "failed to list prompt context sources")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": items, "total": total, "page": page, "page_size": pageSize})
}

func (a *app) promptContextSourceByID(w http.ResponseWriter, r *http.Request) {
	identity, ok := a.requireAuth(w, r)
	if !ok {
		return
	}
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	raw := strings.TrimPrefix(r.URL.Path, "/_workspace/api/v1/prompt-context/sources/")
	parts := strings.Split(strings.Trim(raw, "/"), "/")
	if len(parts) != 2 || parts[0] != "conversation" && parts[0] != "trajectory" {
		writeAPIError(w, http.StatusBadRequest, "invalid prompt context source")
		return
	}
	sourceID, err := strconv.ParseInt(parts[1], 10, 64)
	if err != nil || sourceID <= 0 {
		writeAPIError(w, http.StatusBadRequest, "invalid prompt context source")
		return
	}
	item, messages, err := a.loadPromptContextSource(r, identity.ID, parts[0], sourceID)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			writeAPIError(w, http.StatusNotFound, "prompt context source not found")
		} else {
			writeAPIError(w, http.StatusServiceUnavailable, "failed to load prompt context source")
		}
		return
	}
	trimmed, truncated := trimPromptContextMessages(messages, maxPromptContextMessages, maxPromptContextRunes)
	texts := make([]string, len(trimmed))
	for index := range trimmed {
		texts[index] = trimmed[index].Content
	}
	redacted, entities, err := a.redactMemoryTexts(r.Context(), texts)
	if err != nil {
		writeAPIError(w, http.StatusServiceUnavailable, "privacy filter unavailable")
		return
	}
	for index := range trimmed {
		trimmed[index].ID = fmt.Sprintf("%s-%d-%d", parts[0], sourceID, index+1)
		trimmed[index].Content = redacted[index]
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"source": item, "messages": trimmed, "truncated": truncated, "redacted_entities": entities,
	})
}

func (a *app) loadPromptContextSource(r *http.Request, userID int64, kind string, sourceID int64) (promptContextSourceSummary, []promptContextMessage, error) {
	item := promptContextSourceSummary{Kind: kind, ID: sourceID}
	messages := make([]promptContextMessage, 0, maxPromptContextMessages)
	if kind == "conversation" {
		var raw []byte
		var assistant string
		if err := a.db.QueryRow(r.Context(), `SELECT request_payload,COALESCE(assistant_text,''),COALESCE(model,''),completed_at
			FROM conversation_archives WHERE id=$1 AND user_id=$2`, sourceID, userID).Scan(&raw, &assistant, &item.Model, &item.CompletedAt); err != nil {
			return item, nil, err
		}
		item.Title = firstNonEmpty(item.Model, "Conversation")
		var payload any
		if json.Unmarshal(raw, &payload) == nil {
			messages = extractPromptContextMessages(payload)
		}
		assistant = strings.TrimSpace(assistant)
		if assistant != "" && (len(messages) == 0 || messages[len(messages)-1].Role != "assistant" || messages[len(messages)-1].Content != assistant) {
			messages = append(messages, promptContextMessage{Role: "assistant", Content: assistant})
		}
		item.Preview = truncateRunes(assistant, 240)
		return item, messages, nil
	}
	var finalText string
	if err := a.db.QueryRow(r.Context(), `SELECT COALESCE(title,''),COALESCE(final_text,''),COALESCE(model,''),completed_at
		FROM agent_trajectories WHERE id=$1 AND user_id=$2`, sourceID, userID).Scan(&item.Title, &finalText, &item.Model, &item.CompletedAt); err != nil {
		return item, nil, err
	}
	rows, err := a.db.Query(r.Context(), `SELECT content FROM agent_trajectory_events
		WHERE trajectory_id=$1 AND role='user' ORDER BY sequence_no LIMIT 40`, sourceID)
	if err != nil {
		return item, nil, err
	}
	for rows.Next() {
		var raw []byte
		if rows.Scan(&raw) != nil {
			continue
		}
		var value any
		if json.Unmarshal(raw, &value) == nil {
			if text := extractKnownUserText(value); text != "" {
				messages = append(messages, promptContextMessage{Role: "user", Content: text})
			}
		}
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return item, nil, err
	}
	if finalText = strings.TrimSpace(finalText); finalText != "" {
		messages = append(messages, promptContextMessage{Role: "assistant", Content: finalText})
	}
	item.Title = firstNonEmpty(item.Title, "Agent trajectory")
	item.Preview = truncateRunes(finalText, 240)
	return item, messages, nil
}

func trimPromptContextMessages(messages []promptContextMessage, maxMessages, maxRunes int) ([]promptContextMessage, bool) {
	if maxMessages <= 0 || maxRunes <= 0 {
		return []promptContextMessage{}, len(messages) > 0
	}
	selected := make([]promptContextMessage, 0, minInt(len(messages), maxMessages))
	total := 0
	truncated := false
	for index := len(messages) - 1; index >= 0 && len(selected) < maxMessages; index-- {
		message := messages[index]
		runes := []rune(strings.TrimSpace(message.Content))
		if len(runes) == 0 {
			continue
		}
		remaining := maxRunes - total
		if remaining <= 0 {
			truncated = true
			break
		}
		if len(runes) > remaining {
			message.Content = string(runes[len(runes)-remaining:])
			truncated = true
		}
		total += len([]rune(message.Content))
		selected = append(selected, message)
	}
	if len(selected) < len(messages) {
		truncated = true
	}
	for left, right := 0, len(selected)-1; left < right; left, right = left+1, right-1 {
		selected[left], selected[right] = selected[right], selected[left]
	}
	return selected, truncated
}

func buildContextRetrievalText(messages []promptContextMessage, maxRunes int) string {
	parts := make([]string, 0, len(messages))
	total := 0
	for _, message := range messages {
		part := message.Role + ": " + strings.TrimSpace(message.Content)
		size := len([]rune(part))
		separator := 0
		if len(parts) > 0 {
			separator = 1
		}
		if size == 0 || size+separator > maxRunes || total+size+separator > maxRunes {
			continue
		}
		parts = append(parts, part)
		total += size + separator
	}
	return strings.Join(parts, "\n")
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value = strings.TrimSpace(value); value != "" {
			return value
		}
	}
	return ""
}

func extractPromptContextMessages(value any) []promptContextMessage {
	messages := make([]promptContextMessage, 0, 12)
	var walk func(any, string)
	walk = func(value any, inheritedRole string) {
		switch item := value.(type) {
		case string:
			role := normalizePromptContextRole(inheritedRole)
			text := strings.TrimSpace(item)
			if role != "" && text != "" {
				messages = append(messages, promptContextMessage{Role: role, Content: text})
			}
		case []any:
			for _, child := range item {
				walk(child, inheritedRole)
			}
		case map[string]any:
			role := inheritedRole
			if rawRole, ok := item["role"].(string); ok {
				role = rawRole
			}
			normalizedRole := normalizePromptContextRole(role)
			if role != "" && normalizedRole == "" {
				return
			}
			if normalizedRole != "" {
				for _, key := range []string{"content", "text", "parts"} {
					if child, ok := item[key]; ok {
						walk(child, normalizedRole)
					}
				}
				return
			}
			for _, key := range []string{"messages", "contents"} {
				if child, ok := item[key]; ok {
					walk(child, inheritedRole)
					return
				}
			}
			if child, ok := item["input"]; ok {
				walk(child, "user")
			}
		}
	}
	walk(value, "")
	for index := range messages {
		messages[index].ID = "ctx-" + strconvInt64(int64(index+1))
	}
	return messages
}

func normalizePromptContextRole(role string) string {
	switch strings.ToLower(strings.TrimSpace(role)) {
	case "user":
		return "user"
	case "assistant", "model":
		return "assistant"
	default:
		return ""
	}
}
