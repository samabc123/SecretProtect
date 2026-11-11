package main

import (
	"context"
	"database/sql"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

//go:embed static/admin.html
var archiveAdminHTML []byte

//go:embed static/device.html
var trajectoryDeviceHTML []byte

type adminIdentity struct {
	ID       int64  `json:"id"`
	Username string `json:"username"`
	Email    string `json:"email"`
	Role     string `json:"role"`
	Status   string `json:"status"`
	LabBusinessAccess *bool `json:"lab_business_access"`
}

type authEnvelope struct {
	Code    int           `json:"code"`
	Message string        `json:"message"`
	Data    adminIdentity `json:"data"`
}

type apiEnvelope struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
	Data    any    `json:"data,omitempty"`
}

func (s *proxyServer) serveArchivePlugin(w http.ResponseWriter, req *http.Request) {
	if req.URL.Path == "/_archive/device" {
		http.Redirect(w, req, "/_archive/device/", http.StatusTemporaryRedirect)
		return
	}
	if req.URL.Path == "/_archive/device/" || req.URL.Path == "/_archive/device/index.html" {
		s.serveTrajectoryDevicePage(w, req)
		return
	}
	if strings.HasPrefix(req.URL.Path, "/_archive/api/v1/trajectory-sync/") {
		s.serveTrajectorySync(w, req)
		return
	}
	if req.URL.Path == "/_archive/admin" {
		http.Redirect(w, req, "/_archive/admin/", http.StatusTemporaryRedirect)
		return
	}
	if req.URL.Path == "/_archive/admin/" || req.URL.Path == "/_archive/admin/index.html" {
		if req.Method != http.MethodGet && req.Method != http.MethodHead {
			w.Header().Set("Allow", "GET, HEAD")
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("X-Frame-Options", "DENY")
		if req.Method == http.MethodGet {
			_, _ = w.Write(archiveAdminHTML)
		}
		return
	}
	if !strings.HasPrefix(req.URL.Path, "/_archive/api/v1/") {
		http.NotFound(w, req)
		return
	}
	if req.Method != http.MethodGet {
		writeAPIError(w, http.StatusMethodNotAllowed, 40500, "Method not allowed")
		return
	}
	identity, status, err := s.validateAdmin(req)
	if err != nil {
		s.metrics.adminAuthFailures.Add(1)
		writeAPIError(w, status, status*100, err.Error())
		return
	}
	switch req.URL.Path {
	case "/_archive/api/v1/session":
		writeAPISuccess(w, identity)
	case "/_archive/api/v1/conversation-archives":
		s.listConversationArchives(w, req)
	case "/_archive/api/v1/conversation-keywords":
		s.listConversationKeywords(w, req)
	case "/_archive/api/v1/agent-trajectories":
		s.listAgentTrajectories(w, req)
	default:
		const prefix = "/_archive/api/v1/conversation-archives/"
		if strings.HasPrefix(req.URL.Path, prefix) {
			s.getConversationArchive(w, req, strings.TrimPrefix(req.URL.Path, prefix))
			return
		}
		const trajectoryPrefix = "/_archive/api/v1/agent-trajectories/"
		if strings.HasPrefix(req.URL.Path, trajectoryPrefix) {
			s.serveAgentTrajectory(w, req, strings.TrimPrefix(req.URL.Path, trajectoryPrefix))
			return
		}
		writeAPIError(w, http.StatusNotFound, 40400, "Archive endpoint not found")
	}
}

func (s *proxyServer) validateAdmin(req *http.Request) (adminIdentity, int, error) {
	identity, status, err := s.validateSub2APIUser(req)
	if err != nil {
		return adminIdentity{}, status, err
	}
	if identity.Role != "admin" {
		return adminIdentity{}, http.StatusForbidden, fmt.Errorf("Administrator permission is required")
	}
	return identity, http.StatusOK, nil
}

func (s *proxyServer) validateSub2APIUser(req *http.Request) (adminIdentity, int, error) {
	authorization := strings.TrimSpace(req.Header.Get("Authorization"))
	if !strings.HasPrefix(strings.ToLower(authorization), "bearer ") {
		return adminIdentity{}, http.StatusUnauthorized, fmt.Errorf("Sub2API login is required")
	}
	target := s.cfg.UpstreamURL.ResolveReference(&url.URL{Path: "/api/v1/auth/me"})
	authCtx, authCancel := context.WithTimeout(req.Context(), 3*time.Second)
	defer authCancel()
	authReq, err := http.NewRequestWithContext(authCtx, http.MethodGet, target.String(), nil)
	if err != nil {
		return adminIdentity{}, http.StatusServiceUnavailable, fmt.Errorf("admin authentication is unavailable")
	}
	authReq.Header.Set("Authorization", authorization)
	authReq.Header.Set("Accept", "application/json")
	if language := strings.TrimSpace(req.Header.Get("Accept-Language")); language != "" {
		authReq.Header.Set("Accept-Language", language)
	}
	response, err := s.adminClient.Do(authReq)
	if err != nil {
		return adminIdentity{}, http.StatusServiceUnavailable, fmt.Errorf("Sub2API authentication check failed")
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode == http.StatusUnauthorized {
		return adminIdentity{}, http.StatusUnauthorized, fmt.Errorf("Sub2API login has expired")
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return adminIdentity{}, http.StatusServiceUnavailable, fmt.Errorf("Sub2API authentication check returned status %d", response.StatusCode)
	}
	var envelope authEnvelope
	if err := json.NewDecoder(io.LimitReader(response.Body, 1<<20)).Decode(&envelope); err != nil {
		return adminIdentity{}, http.StatusServiceUnavailable, fmt.Errorf("Sub2API authentication response is invalid")
	}
	if envelope.Code != 0 || envelope.Data.ID <= 0 {
		return adminIdentity{}, http.StatusUnauthorized, fmt.Errorf("Sub2API login is invalid")
	}
	if envelope.Data.Status != "active" {
		return adminIdentity{}, http.StatusForbidden, fmt.Errorf("Active Sub2API account is required")
	}
	if envelope.Data.LabBusinessAccess != nil && !*envelope.Data.LabBusinessAccess ||
		os.Getenv("LAB_ADMISSION_ENABLED") == "1" && envelope.Data.LabBusinessAccess == nil {
		return adminIdentity{}, http.StatusForbidden, fmt.Errorf("Laboratory business admission is required")
	}
	return envelope.Data, http.StatusOK, nil
}

func (s *proxyServer) listConversationArchives(w http.ResponseWriter, req *http.Request) {
	query, err := parseArchiveQuery(req.URL.Query())
	if err != nil {
		writeAPIError(w, http.StatusBadRequest, 40001, err.Error())
		return
	}
	ctx, cancel := contextWithTimeout(req, s.cfg.DatabaseTimeout)
	defer cancel()
	page, err := s.store.ListArchives(ctx, query)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, 50001, "Failed to query conversation archives")
		return
	}
	writeAPISuccess(w, page)
}

func (s *proxyServer) listConversationKeywords(w http.ResponseWriter, req *http.Request) {
	days, limit := 30, 60
	if raw := strings.TrimSpace(req.URL.Query().Get("days")); raw != "" {
		value, err := strconv.Atoi(raw)
		if err != nil || (value != 7 && value != 30) {
			writeAPIError(w, http.StatusBadRequest, 40021, "Invalid days; expected 7 or 30")
			return
		}
		days = value
	}
	if raw := strings.TrimSpace(req.URL.Query().Get("limit")); raw != "" {
		value, err := strconv.Atoi(raw)
		if err != nil || value < 10 || value > 80 {
			writeAPIError(w, http.StatusBadRequest, 40022, "Invalid limit; expected 10-80")
			return
		}
		limit = value
	}
	store, ok := s.store.(archiveKeywordStore)
	if !ok {
		writeAPIError(w, http.StatusServiceUnavailable, 50321, "Keyword statistics are unavailable")
		return
	}
	ctx, cancel := contextWithTimeout(req, 15*time.Second)
	defer cancel()
	stats, err := store.ListArchiveKeywordStats(ctx, days, limit)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, 50021, "Failed to query keyword statistics")
		return
	}
	writeAPISuccess(w, stats)
}

func (s *proxyServer) getConversationArchive(w http.ResponseWriter, req *http.Request, rawID string) {
	id, err := strconv.ParseInt(strings.TrimSpace(rawID), 10, 64)
	if err != nil || id <= 0 {
		writeAPIError(w, http.StatusBadRequest, 40002, "Invalid conversation archive id")
		return
	}
	ctx, cancel := contextWithTimeout(req, s.cfg.DatabaseTimeout)
	defer cancel()
	item, err := s.store.GetArchive(ctx, id)
	if errors.Is(err, sql.ErrNoRows) {
		writeAPIError(w, http.StatusNotFound, 40401, "Conversation archive not found")
		return
	}
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, 50002, "Failed to read conversation archive")
		return
	}
	writeAPISuccess(w, item)
}

func (s *proxyServer) listAgentTrajectories(w http.ResponseWriter, req *http.Request) {
	query, err := parseTrajectoryQuery(req.URL.Query())
	if err != nil {
		writeAPIError(w, http.StatusBadRequest, 40011, err.Error())
		return
	}
	ctx, cancel := contextWithTimeout(req, s.cfg.DatabaseTimeout)
	defer cancel()
	page, err := s.store.ListTrajectories(ctx, query)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, 50011, "Failed to query agent trajectories")
		return
	}
	writeAPISuccess(w, page)
}

func (s *proxyServer) serveAgentTrajectory(w http.ResponseWriter, req *http.Request, suffix string) {
	parts := strings.Split(strings.Trim(suffix, "/"), "/")
	id, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil || id <= 0 {
		writeAPIError(w, http.StatusBadRequest, 40012, "Invalid agent trajectory id")
		return
	}
	ctx, cancel := contextWithTimeout(req, s.cfg.DatabaseTimeout)
	defer cancel()
	if len(parts) == 2 && parts[1] == "events" {
		page, pageSize, err := parsePage(req.URL.Query(), 500)
		if err != nil {
			writeAPIError(w, http.StatusBadRequest, 40013, err.Error())
			return
		}
		if _, err := s.store.GetTrajectory(ctx, id); errors.Is(err, sql.ErrNoRows) {
			writeAPIError(w, http.StatusNotFound, 40411, "Agent trajectory not found")
			return
		} else if err != nil {
			writeAPIError(w, http.StatusInternalServerError, 50012, "Failed to read agent trajectory")
			return
		}
		events, err := s.store.ListTrajectoryEvents(ctx, id, page, pageSize)
		if err != nil {
			writeAPIError(w, http.StatusInternalServerError, 50013, "Failed to read agent trajectory events")
			return
		}
		writeAPISuccess(w, events)
		return
	}
	if len(parts) != 1 {
		writeAPIError(w, http.StatusNotFound, 40412, "Agent trajectory endpoint not found")
		return
	}
	item, err := s.store.GetTrajectory(ctx, id)
	if errors.Is(err, sql.ErrNoRows) {
		writeAPIError(w, http.StatusNotFound, 40411, "Agent trajectory not found")
		return
	}
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, 50012, "Failed to read agent trajectory")
		return
	}
	writeAPISuccess(w, item)
}

func parseArchiveQuery(values url.Values) (archiveQuery, error) {
	query := archiveQuery{Page: 1, PageSize: 50}
	if raw := strings.TrimSpace(values.Get("page")); raw != "" {
		value, err := strconv.Atoi(raw)
		if err != nil || value <= 0 {
			return archiveQuery{}, fmt.Errorf("Invalid page")
		}
		query.Page = value
	}
	if raw := strings.TrimSpace(values.Get("page_size")); raw != "" {
		value, err := strconv.Atoi(raw)
		if err != nil || value <= 0 || value > 200 {
			return archiveQuery{}, fmt.Errorf("Invalid page_size; expected 1-200")
		}
		query.PageSize = value
	}
	for name, target := range map[string]**int64{"user_id": &query.UserID, "api_key_id": &query.APIKeyID} {
		raw := strings.TrimSpace(values.Get(name))
		if raw == "" {
			continue
		}
		value, err := strconv.ParseInt(raw, 10, 64)
		if err != nil || value <= 0 {
			return archiveQuery{}, fmt.Errorf("Invalid %s", name)
		}
		*target = &value
	}
	query.Model = strings.TrimSpace(values.Get("model"))
	query.Protocol = strings.TrimSpace(values.Get("protocol"))
	for name, target := range map[string]**time.Time{"start_time": &query.StartTime, "end_time": &query.EndTime} {
		raw := strings.TrimSpace(values.Get(name))
		if raw == "" {
			continue
		}
		value, err := time.Parse(time.RFC3339, raw)
		if err != nil {
			return archiveQuery{}, fmt.Errorf("Invalid %s; expected RFC3339", name)
		}
		value = value.UTC()
		*target = &value
	}
	return query, nil
}

func parsePage(values url.Values, maxPageSize int) (int, int, error) {
	page, pageSize := 1, 100
	if raw := strings.TrimSpace(values.Get("page")); raw != "" {
		value, err := strconv.Atoi(raw)
		if err != nil || value <= 0 {
			return 0, 0, fmt.Errorf("Invalid page")
		}
		page = value
	}
	if raw := strings.TrimSpace(values.Get("page_size")); raw != "" {
		value, err := strconv.Atoi(raw)
		if err != nil || value <= 0 || value > maxPageSize {
			return 0, 0, fmt.Errorf("Invalid page_size; expected 1-%d", maxPageSize)
		}
		pageSize = value
	}
	return page, pageSize, nil
}

func parseTrajectoryQuery(values url.Values) (trajectoryQuery, error) {
	page, pageSize, err := parsePage(values, 200)
	if err != nil {
		return trajectoryQuery{}, err
	}
	query := trajectoryQuery{Page: page, PageSize: pageSize}
	if raw := strings.TrimSpace(values.Get("user_id")); raw != "" {
		value, err := strconv.ParseInt(raw, 10, 64)
		if err != nil || value <= 0 {
			return trajectoryQuery{}, fmt.Errorf("Invalid user_id")
		}
		query.UserID = &value
	}
	query.Status = strings.TrimSpace(values.Get("status"))
	query.Model = strings.TrimSpace(values.Get("model"))
	query.Source = strings.TrimSpace(values.Get("source"))
	query.ToolName = strings.TrimSpace(values.Get("tool_name"))
	for name, target := range map[string]**time.Time{"start_time": &query.StartTime, "end_time": &query.EndTime} {
		raw := strings.TrimSpace(values.Get(name))
		if raw == "" {
			continue
		}
		value, err := time.Parse(time.RFC3339, raw)
		if err != nil {
			return trajectoryQuery{}, fmt.Errorf("Invalid %s; expected RFC3339", name)
		}
		value = value.UTC()
		*target = &value
	}
	return query, nil
}

func contextWithTimeout(req *http.Request, timeout time.Duration) (context.Context, context.CancelFunc) {
	return context.WithTimeout(req.Context(), timeout)
}

func writeAPISuccess(w http.ResponseWriter, data any) {
	writeAPI(w, http.StatusOK, apiEnvelope{Code: 0, Message: "success", Data: data})
}

func writeAPIError(w http.ResponseWriter, status, code int, message string) {
	writeAPI(w, status, apiEnvelope{Code: code, Message: message})
}

func writeAPI(w http.ResponseWriter, status int, body apiEnvelope) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}
