package main

import (
	"context"
	"sort"
	"strings"
	"time"
)

type knowledgeCitation struct {
	KnowledgeType string    `json:"knowledge_type"`
	Title         string    `json:"title"`
	DocumentID    int64     `json:"document_id"`
	Version       int       `json:"version"`
	ChunkID       int64     `json:"chunk_id"`
	Scope         string    `json:"scope"`
	Content       string    `json:"-"`
	UpdatedAt     time.Time `json:"-"`
}

type rankedKnowledgeChunk struct {
	Citation knowledgeCitation
	Score    float64
}

func effectiveRAGSources(settings promptRAGSettingsValue, requested []string) []string {
	seen := map[string]bool{}
	result := make([]string, 0, 2)
	for _, source := range requested {
		if seen[source] {
			continue
		}
		if source == "prompt_library" && settings.UsePromptLibrary || source == "weekly_report" && settings.UseWeeklyReports {
			seen[source] = true
			result = append(result, source)
		}
	}
	return result
}

func (a *app) retrieveKnowledgeMemories(ctx context.Context, userID int64, sources []string, prompt string, contextMessages []promptContextMessage, client promptModelClient) ([]retrievedMemory, map[int64]knowledgeCitation, []string) {
	if len(sources) == 0 {
		return nil, map[int64]knowledgeCitation{}, nil
	}
	query := truncateExactRunes(strings.TrimSpace(prompt+"\n"+buildContextRetrievalText(contextMessages, 4000)), 12000)
	vector, embeddingErr := client.embed(ctx, query)
	warnings := make([]string, 0)
	if embeddingErr != nil {
		warnings = append(warnings, "knowledge_embedding_fallback")
	}
	selected := make([]rankedKnowledgeChunk, 0, len(sources)*4)
	for _, source := range sources {
		ranked, errs := a.retrieveKnowledgeType(ctx, userID, source, query, vector)
		warnings = append(warnings, errs...)
		selected = append(selected, ranked...)
	}
	sort.Slice(selected, func(i, j int) bool {
		if selected[i].Citation.KnowledgeType != selected[j].Citation.KnowledgeType {
			return selected[i].Citation.KnowledgeType < selected[j].Citation.KnowledgeType
		}
		return selected[i].Score > selected[j].Score
	})
	perType := map[string]int{}
	totalRunes := 0
	memories := make([]retrievedMemory, 0, len(selected))
	citations := make(map[int64]knowledgeCitation, len(selected))
	for _, item := range selected {
		if perType[item.Citation.KnowledgeType] >= 4 {
			continue
		}
		content := truncateExactRunes(item.Citation.Content, 3000)
		if totalRunes+len([]rune(content)) > 7000 {
			continue
		}
		syntheticID := -item.Citation.ChunkID
		if syntheticID >= 0 {
			continue
		}
		item.Citation.Content = content
		citations[syntheticID] = item.Citation
		memories = append(memories, retrievedMemory{ID: syntheticID, Type: "knowledge_" + item.Citation.KnowledgeType,
			Title: item.Citation.Title, Content: content, Confidence: 1, Importance: 3, LastSeenAt: item.Citation.UpdatedAt})
		perType[item.Citation.KnowledgeType]++
		totalRunes += len([]rune(content))
	}
	return memories, citations, uniqueStrings(warnings)
}

func (a *app) retrieveKnowledgeType(ctx context.Context, userID int64, knowledgeType, query string, vector []float64) ([]rankedKnowledgeChunk, []string) {
	warnings := make([]string, 0, 2)
	lists := make([][]knowledgeCitation, 0, 2)
	if len(vector) > 0 {
		items, err := a.queryKnowledgeCitations(ctx, `SELECT c.id,d.id,d.knowledge_type,d.title,v.version,d.visibility,c.content,d.updated_at
			FROM knowledge_documents d JOIN knowledge_document_versions v ON v.id=d.active_version_id
			JOIN knowledge_chunks c ON c.document_version_id=v.id
			WHERE d.knowledge_type=$1 AND d.enabled AND v.status='ready' AND c.embedding IS NOT NULL AND (
				d.visibility='private' AND d.owner_user_id=$2 AND d.review_status='private' OR
				d.visibility='shared' AND d.review_status='approved')
			ORDER BY c.embedding <=> $3::vector LIMIT 20`, knowledgeType, userID, vectorLiteral(vector))
		if err != nil {
			warnings = append(warnings, knowledgeType+"_semantic_retrieval_failed")
		} else {
			lists = append(lists, items)
		}
	}
	items, err := a.queryKnowledgeCitations(ctx, `SELECT c.id,d.id,d.knowledge_type,d.title,v.version,d.visibility,c.content,d.updated_at
		FROM knowledge_documents d JOIN knowledge_document_versions v ON v.id=d.active_version_id
		JOIN knowledge_chunks c ON c.document_version_id=v.id
		WHERE d.knowledge_type=$1 AND d.enabled AND v.status='ready' AND (
			d.visibility='private' AND d.owner_user_id=$2 AND d.review_status='private' OR
			d.visibility='shared' AND d.review_status='approved')
		ORDER BY similarity(c.search_text,$3) DESC,d.updated_at DESC LIMIT 20`, knowledgeType, userID, query)
	if err != nil {
		warnings = append(warnings, knowledgeType+"_lexical_retrieval_failed")
	} else {
		lists = append(lists, items)
	}
	scores := map[int64]float64{}
	values := map[int64]knowledgeCitation{}
	for listIndex, list := range lists {
		weight := 1.0
		if listIndex > 0 && len(vector) > 0 {
			weight = 0.8
		}
		for rank, item := range list {
			scores[item.ChunkID] += weight / float64(60+rank+1)
			values[item.ChunkID] = item
		}
	}
	result := make([]rankedKnowledgeChunk, 0, len(values))
	for id, value := range values {
		result = append(result, rankedKnowledgeChunk{Citation: value, Score: scores[id]})
	}
	sort.Slice(result, func(i, j int) bool { return result[i].Score > result[j].Score })
	if len(result) > 12 {
		result = result[:12]
	}
	return result, warnings
}

func (a *app) queryKnowledgeCitations(ctx context.Context, query string, args ...any) ([]knowledgeCitation, error) {
	rows, err := a.db.Query(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := make([]knowledgeCitation, 0)
	for rows.Next() {
		var item knowledgeCitation
		if err := rows.Scan(&item.ChunkID, &item.DocumentID, &item.KnowledgeType, &item.Title, &item.Version, &item.Scope, &item.Content, &item.UpdatedAt); err != nil {
			return nil, err
		}
		result = append(result, item)
	}
	return result, rows.Err()
}
