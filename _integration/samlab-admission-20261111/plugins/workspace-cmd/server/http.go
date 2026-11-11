package main

import (
	"encoding/json"
	"fmt"
	"io"
	"mime"
	"net/http"
	"net/url"
	"path/filepath"
	"strings"
)

func (a *app) security(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'")
		w.Header().Set("X-Frame-Options", "SAMEORIGIN")
		next.ServeHTTP(w, r)
	})
}

func (a *app) health(w http.ResponseWriter, r *http.Request) {
	a.memoryMu.RLock()
	lastSuccess := any(nil)
	if !a.memoryLastSuccess.IsZero() {
		lastSuccess = a.memoryLastSuccess
	}
	lastConsolidation := any(nil)
	if !a.memoryLastConsolidation.IsZero() {
		lastConsolidation = a.memoryLastConsolidation
	}
	memory := map[string]any{
		"worker_enabled":        a.memoryWorkerConfigured(),
		"backlog":               a.memoryBacklog,
		"processed":             a.memoryProcessed,
		"failures":              a.memoryFailures,
		"last_success_at":       lastSuccess,
		"last_consolidation_at": lastConsolidation,
		"has_recent_error":      a.memoryLastError != "",
		"worker_interval_secs":  int64(a.cfg.MemoryWorkerInterval.Seconds()),
	}
	a.memoryMu.RUnlock()
	a.knowledgeMu.RLock()
	knowledgeLastSuccess := any(nil)
	if !a.knowledgeLastSuccess.IsZero() {
		knowledgeLastSuccess = a.knowledgeLastSuccess
	}
	knowledge := map[string]any{
		"worker_enabled":       a.knowledgeWorkerConfigured(),
		"backlog":              a.knowledgeBacklog,
		"processed":            a.knowledgeProcessed,
		"failures":             a.knowledgeFailures,
		"last_success_at":      knowledgeLastSuccess,
		"has_recent_error":     a.knowledgeLastError != "",
		"worker_interval_secs": int64(a.cfg.KnowledgeWorkerInterval.Seconds()),
	}
	a.knowledgeMu.RUnlock()
	vectorReady := false
	if a.db != nil {
		_ = a.db.QueryRow(r.Context(), `SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector')`).Scan(&vectorReady)
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status":                    "ok",
		"plugin_version":            "workspace-v5-knowledge-rag",
		"deepseek_configured":       a.cfg.DeepSeekKey != "",
		"deepseek_model":            a.cfg.DeepSeekModel,
		"prompt_model_configured":   a.cfg.PromptModelAPIKey != "" && a.cfg.PromptPolishModel != "",
		"prompt_model":              a.cfg.PromptPolishModel,
		"embedding_configured":      a.cfg.PromptEmbeddingBaseURL != "" && a.cfg.PromptEmbeddingAPIKey != "" && a.cfg.PromptEmbeddingModel != "",
		"embedding_model":           a.cfg.PromptEmbeddingModel,
		"embedding_dimensions":      a.cfg.PromptEmbeddingDimensions,
		"privacy_filter_configured": a.cfg.PrivacyFilterURL != "",
		"vector_extension_ready":    vectorReady,
		"memory":                    memory,
		"knowledge":                 knowledge,
	})
}

func (a *app) static(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	path := strings.TrimPrefix(r.URL.Path, "/_workspace")
	if path == "" || path == "/" || path == "/weekly" || path == "/files" || path == "/prompt" {
		path = "/index.html"
	}
	path = strings.TrimPrefix(path, "/")
	if path == "" || strings.Contains(path, "..") || strings.ContainsAny(path, "\\\x00") {
		http.NotFound(w, r)
		return
	}
	data, err := webFS.ReadFile("web/" + path)
	if err != nil {
		http.NotFound(w, r)
		return
	}
	if contentType := mime.TypeByExtension(filepath.Ext(path)); contentType != "" {
		w.Header().Set("Content-Type", contentType)
	}
	if strings.HasSuffix(path, ".html") {
		w.Header().Set("Cache-Control", "no-store")
	} else {
		w.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
	}
	if r.Method == http.MethodGet {
		_, _ = w.Write(data)
	}
}

func writeJSON(w http.ResponseWriter, status int, data any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	if _, ok := data.(envelope); !ok {
		data = envelope{Code: 0, Message: "success", Data: data}
	}
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(data)
}

func writeAPIError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, envelope{Code: 1, Message: message})
}

func readJSON(r *http.Request, dst any, limit int64) error {
	defer r.Body.Close()
	decoder := json.NewDecoder(io.LimitReader(r.Body, limit))
	decoder.DisallowUnknownFields()
	return decoder.Decode(dst)
}

func truncateRunes(value string, limit int) string {
	value = strings.TrimSpace(value)
	runes := []rune(value)
	if len(runes) <= limit {
		return value
	}
	return string(runes[:limit]) + "…"
}

func safeFilename(name string) string {
	name = strings.ReplaceAll(name, "\\", "/")
	name = filepath.Base(name)
	name = strings.TrimSpace(name)
	if name == "." || name == "" || strings.ContainsRune(name, '\x00') {
		return "file"
	}
	return truncateRunes(name, 255)
}

func contentDisposition(disposition, name string) string {
	return fmt.Sprintf("%s; filename*=UTF-8''%s", disposition, url.PathEscape(name))
}
