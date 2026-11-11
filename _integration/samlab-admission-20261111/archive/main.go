package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"
)

const (
	archiveUserIDHeader   = "X-Sub2API-Archive-User-ID"
	archiveAPIKeyIDHeader = "X-Sub2API-Archive-API-Key-ID"
	clientRequestIDHeader = "X-Client-Request-ID"
	archivePluginVersion  = "external-v8-keyword-snapshots"
)

type config struct {
	ListenAddr                  string
	UpstreamURL                 *url.URL
	PrivacyFilterURL            *url.URL
	PrivacyTimeout              time.Duration
	PrivacyMaxBody              int64
	DatabaseURL                 string
	TrajectoryUploadDatabaseURL string
	Retention                   time.Duration
	RetentionSweepEnabled       bool
	SweepInterval               time.Duration
	SweepBatchSize              int
	DatabaseTimeout             time.Duration
	IdentityResolveTimeout      time.Duration
	ResolveInterval             time.Duration
	ResolveBatchSize            int
	AdminAuthTimeout            time.Duration
	UploadDeviceTTL             time.Duration
	UploadSessionTTL            time.Duration
	UploadPollInterval          time.Duration
	UploadMaxBytes              int64
	UploadMaxRecordBytes        int64
	ArchivePublicURL            *url.URL
}

func loadConfig() (config, error) {
	upstream, err := url.Parse(envOr("UPSTREAM_URL", "http://127.0.0.1:8080"))
	if err != nil || upstream.Scheme == "" || upstream.Host == "" {
		return config{}, fmt.Errorf("invalid UPSTREAM_URL")
	}
	privacyFilter, err := url.Parse(envOr("PRIVACY_FILTER_URL", "http://privacy-filter:8088"))
	if err != nil || privacyFilter.Scheme == "" || privacyFilter.Host == "" {
		return config{}, fmt.Errorf("invalid PRIVACY_FILTER_URL")
	}
	privacyTimeout, err := positiveDuration("PRIVACY_FILTER_TIMEOUT", "10s")
	if err != nil {
		return config{}, err
	}
	databaseTimeout, err := positiveDuration("DATABASE_TIMEOUT", "5s")
	if err != nil {
		return config{}, err
	}
	identityResolveTimeout, err := positiveDuration("IDENTITY_RESOLVE_TIMEOUT", "30s")
	if err != nil {
		return config{}, err
	}
	resolveInterval, err := positiveDuration("IDENTITY_RESOLVE_INTERVAL", "1s")
	if err != nil {
		return config{}, err
	}
	adminAuthTimeout, err := positiveDuration("ADMIN_AUTH_TIMEOUT", "5s")
	if err != nil {
		return config{}, err
	}
	uploadDeviceTTL, err := positiveDuration("UPLOAD_DEVICE_TTL", "10m")
	if err != nil {
		return config{}, err
	}
	uploadSessionTTL, err := positiveDuration("UPLOAD_SESSION_TTL", "2h")
	if err != nil {
		return config{}, err
	}
	uploadPollInterval, err := positiveDuration("UPLOAD_POLL_INTERVAL", "3s")
	if err != nil {
		return config{}, err
	}
	sweepInterval, err := positiveDuration("SWEEP_INTERVAL", "1h")
	if err != nil {
		return config{}, err
	}
	privacyMaxBody, err := positiveInt64("PRIVACY_MAX_BODY_BYTES", "268435456")
	if err != nil {
		return config{}, err
	}
	retentionDays, err := positiveInt64("RETENTION_DAYS", "30")
	if err != nil {
		return config{}, err
	}
	if retentionDays != 30 {
		return config{}, fmt.Errorf("RETENTION_DAYS must be exactly 30")
	}
	retentionSweepEnabled, err := booleanEnv("RETENTION_SWEEP_ENABLED", true)
	if err != nil {
		return config{}, err
	}
	sweepBatchSize, err := positiveInt64("SWEEP_BATCH_SIZE", "5000")
	if err != nil {
		return config{}, err
	}
	resolveBatchSize, err := positiveInt64("IDENTITY_RESOLVE_BATCH_SIZE", "100")
	if err != nil {
		return config{}, err
	}
	uploadMaxBytes, err := positiveInt64("UPLOAD_MAX_BYTES", "2147483648")
	if err != nil {
		return config{}, err
	}
	uploadMaxRecordBytes, err := positiveInt64("UPLOAD_MAX_RECORD_BYTES", "16777216")
	if err != nil {
		return config{}, err
	}
	if uploadMaxRecordBytes > maxTrajectoryRecordBytes {
		return config{}, fmt.Errorf("UPLOAD_MAX_RECORD_BYTES cannot exceed %d", maxTrajectoryRecordBytes)
	}
	databaseURL := strings.TrimSpace(os.Getenv("DATABASE_URL"))
	if databaseURL == "" {
		return config{}, fmt.Errorf("DATABASE_URL is required")
	}
	uploadDatabaseURL := strings.TrimSpace(os.Getenv("TRAJECTORY_UPLOAD_DATABASE_URL"))
	if uploadDatabaseURL == "" {
		// The private one-shot importer historically sets only DATABASE_URL.
		// The long-running proxy deployment supplies a separate least-privilege URL.
		uploadDatabaseURL = databaseURL
	}
	var archivePublicURL *url.URL
	if raw := strings.TrimSpace(os.Getenv("ARCHIVE_PUBLIC_URL")); raw != "" {
		archivePublicURL, err = url.Parse(raw)
		if err != nil || archivePublicURL.Scheme != "https" || archivePublicURL.Host == "" {
			return config{}, fmt.Errorf("ARCHIVE_PUBLIC_URL must be an HTTPS origin")
		}
		archivePublicURL.Path = ""
		archivePublicURL.RawQuery = ""
		archivePublicURL.Fragment = ""
	}
	return config{
		ListenAddr:                  envOr("LISTEN_ADDR", ":80"),
		UpstreamURL:                 upstream,
		PrivacyFilterURL:            privacyFilter,
		PrivacyTimeout:              privacyTimeout,
		PrivacyMaxBody:              privacyMaxBody,
		DatabaseURL:                 databaseURL,
		TrajectoryUploadDatabaseURL: uploadDatabaseURL,
		Retention:                   time.Duration(retentionDays) * 24 * time.Hour,
		RetentionSweepEnabled:       retentionSweepEnabled,
		SweepInterval:               sweepInterval,
		SweepBatchSize:              int(sweepBatchSize),
		DatabaseTimeout:             databaseTimeout,
		IdentityResolveTimeout:      identityResolveTimeout,
		ResolveInterval:             resolveInterval,
		ResolveBatchSize:            int(resolveBatchSize),
		AdminAuthTimeout:            adminAuthTimeout,
		UploadDeviceTTL:             uploadDeviceTTL,
		UploadSessionTTL:            uploadSessionTTL,
		UploadPollInterval:          uploadPollInterval,
		UploadMaxBytes:              uploadMaxBytes,
		UploadMaxRecordBytes:        uploadMaxRecordBytes,
		ArchivePublicURL:            archivePublicURL,
	}, nil
}

func envOr(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func booleanEnv(key string, fallback bool) (bool, error) {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback, nil
	}
	parsed, err := strconv.ParseBool(value)
	if err != nil {
		return false, fmt.Errorf("%s must be a boolean", key)
	}
	return parsed, nil
}

func positiveDuration(key, fallback string) (time.Duration, error) {
	value, err := time.ParseDuration(envOr(key, fallback))
	if err != nil || value <= 0 {
		return 0, fmt.Errorf("%s must be a positive duration", key)
	}
	return value, nil
}

func positiveInt64(key, fallback string) (int64, error) {
	value, err := strconv.ParseInt(envOr(key, fallback), 10, 64)
	if err != nil || value <= 0 {
		return 0, fmt.Errorf("%s must be a positive integer", key)
	}
	return value, nil
}

type proxyMetrics struct {
	queued            atomic.Int64
	inserted          atomic.Int64
	insertFailures    atomic.Int64
	discardedStreams  atomic.Int64
	deleted           atomic.Int64
	identityResolved  atomic.Int64
	identityRetried   atomic.Int64
	identityTimeouts  atomic.Int64
	identityAmbiguous atomic.Int64
	identityFailures  atomic.Int64
	missingClientIDs  atomic.Int64
	compatFailures    atomic.Int64
	adminAuthFailures atomic.Int64
	privacyFailures   atomic.Int64
	uploadAuthorized  atomic.Int64
	uploadSessions    atomic.Int64
	uploadRecords     atomic.Int64
	uploadReplays     atomic.Int64
	uploadFailures    atomic.Int64
	uploadRejected    atomic.Int64
}

type archiveContextKey struct{}

type archiveClientWriter struct {
	http.ResponseWriter
	record *pendingArchive
}

func (w *archiveClientWriter) Write(payload []byte) (int, error) {
	written, err := w.ResponseWriter.Write(payload)
	if err != nil || written != len(payload) {
		w.record.clientWriteFailed.Store(true)
	}
	return written, err
}

func (w *archiveClientWriter) Flush() {
	if flusher, ok := w.ResponseWriter.(http.Flusher); ok {
		flusher.Flush()
	}
}

func (w *archiveClientWriter) FlushError() error {
	if flusher, ok := w.ResponseWriter.(interface{ FlushError() error }); ok {
		err := flusher.FlushError()
		if err != nil {
			w.record.clientWriteFailed.Store(true)
		}
		return err
	}
	w.Flush()
	return nil
}

func (w *archiveClientWriter) Unwrap() http.ResponseWriter {
	return w.ResponseWriter
}

type proxyServer struct {
	cfg          config
	store        archiveStore
	uploadStore  trajectoryUploadStore
	privacy      *privacyClient
	proxy        *httputil.ReverseProxy
	metrics      proxyMetrics
	resolverWake chan struct{}
	adminClient  *http.Client
}

func newProxyServer(cfg config, store archiveStore) *proxyServer {
	var uploadStore trajectoryUploadStore
	if candidate, ok := store.(trajectoryUploadStore); ok {
		uploadStore = candidate
	}
	return newProxyServerWithUpload(cfg, store, uploadStore)
}

func newProxyServerWithUpload(cfg config, store archiveStore, uploadStore trajectoryUploadStore) *proxyServer {
	proxy := httputil.NewSingleHostReverseProxy(cfg.UpstreamURL)
	proxy.FlushInterval = -1
	server := &proxyServer{
		cfg: cfg, store: store, uploadStore: uploadStore, privacy: newPrivacyClient(cfg), proxy: proxy,
		resolverWake: make(chan struct{}, 1),
		adminClient: &http.Client{
			Timeout:   cfg.AdminAuthTimeout,
			Transport: &http.Transport{Proxy: nil},
		},
	}
	proxy.ModifyResponse = server.modifyResponse
	proxy.ErrorHandler = server.proxyError
	return server
}

func (s *proxyServer) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	if req.Method == http.MethodGet && req.URL.Path == "/_archive/health" {
		s.health(w, req)
		return
	}
	if strings.HasPrefix(req.URL.Path, "/_archive/admin") ||
		strings.HasPrefix(req.URL.Path, "/_archive/device") ||
		strings.HasPrefix(req.URL.Path, "/_archive/api/") {
		s.serveArchivePlugin(w, req)
		return
	}
	// These headers are owned by Sub2API/the archive boundary. Never forward a
	// client-supplied value, including on routes that are not archived.
	req.Header.Del(archiveUserIDHeader)
	req.Header.Del(archiveAPIKeyIDHeader)
	req.Header.Del(clientRequestIDHeader)
	if !shouldArchive(req) {
		s.proxy.ServeHTTP(w, req)
		return
	}
	if req.Method == http.MethodGet && strings.EqualFold(req.Header.Get("Upgrade"), "websocket") {
		http.Error(w, "websocket conversations are disabled by privacy policy; use HTTP/SSE", http.StatusNotImplemented)
		return
	}

	rawBody, err := readConversationRequest(req.Body, s.cfg.PrivacyMaxBody)
	if err != nil {
		status := http.StatusBadRequest
		if errors.Is(err, errBodyTooLarge) {
			status = http.StatusRequestEntityTooLarge
		}
		http.Error(w, "invalid conversation request", status)
		return
	}
	if err := validateConversationJSON(rawBody); err != nil {
		http.Error(w, "invalid conversation request", http.StatusBadRequest)
		return
	}

	req.Body = io.NopCloser(bytes.NewReader(rawBody))
	req.ContentLength = int64(len(rawBody))
	req.Header.Del("Content-Length")

	protocol := protocolForPath(req.URL.Path)
	record := newPendingArchive(s, req, rawBody, protocol)
	req = req.WithContext(context.WithValue(req.Context(), archiveContextKey{}, record))
	s.proxy.ServeHTTP(&archiveClientWriter{ResponseWriter: w, record: record}, req)
}

func (s *proxyServer) health(w http.ResponseWriter, req *http.Request) {
	privacyStatus := "ok"
	privacyCtx, privacyCancel := context.WithTimeout(req.Context(), s.cfg.PrivacyTimeout)
	if err := s.privacy.health(privacyCtx); err != nil {
		privacyStatus = "degraded"
	}
	privacyCancel()
	if s.store == nil {
		http.Error(w, "archive database unavailable", http.StatusServiceUnavailable)
		return
	}
	databaseCtx, databaseCancel := context.WithTimeout(req.Context(), s.cfg.DatabaseTimeout)
	defer databaseCancel()
	if err := s.store.Ping(databaseCtx); err != nil {
		http.Error(w, "archive database unavailable", http.StatusServiceUnavailable)
		return
	}
	if err := s.store.CheckCompatibility(databaseCtx); err != nil {
		s.metrics.compatFailures.Add(1)
		http.Error(w, "archive compatibility check failed", http.StatusServiceUnavailable)
		return
	}
	if s.cfg.TrajectoryUploadDatabaseURL != "" {
		if s.uploadStore == nil {
			http.Error(w, "trajectory upload database unavailable", http.StatusServiceUnavailable)
			return
		}
		uploadCtx, uploadCancel := context.WithTimeout(req.Context(), s.cfg.DatabaseTimeout)
		if err := s.uploadStore.CheckTrajectoryUploadCompatibility(uploadCtx); err != nil {
			uploadCancel()
			http.Error(w, "trajectory upload compatibility check failed", http.StatusServiceUnavailable)
			return
		}
		uploadCancel()
	}
	w.Header().Set("Content-Type", "application/json")
	_, _ = fmt.Fprintf(w,
		`{"status":"ok","plugin_version":%q,"retention_days":30,"retention_sweep_enabled":%t,"privacy_filter":%q,"database":"ok","queued":%d,"inserted":%d,"insert_failures":%d,"discarded_streams":%d,"identity_resolved":%d,"identity_retried":%d,"identity_timeouts":%d,"identity_ambiguous":%d,"identity_failures":%d,"missing_client_request_ids":%d,"compatibility_failures":%d,"admin_auth_failures":%d,"privacy_failures":%d,"upload_authorized":%d,"upload_sessions":%d,"upload_records":%d,"upload_replays":%d,"upload_failures":%d,"upload_rejected":%d,"deleted":%d}`+"\n",
		archivePluginVersion, s.cfg.RetentionSweepEnabled, privacyStatus,
		s.metrics.queued.Load(), s.metrics.inserted.Load(), s.metrics.insertFailures.Load(),
		s.metrics.discardedStreams.Load(), s.metrics.identityResolved.Load(),
		s.metrics.identityRetried.Load(), s.metrics.identityTimeouts.Load(),
		s.metrics.identityAmbiguous.Load(), s.metrics.identityFailures.Load(),
		s.metrics.missingClientIDs.Load(), s.metrics.compatFailures.Load(),
		s.metrics.adminAuthFailures.Load(), s.metrics.privacyFailures.Load(),
		s.metrics.uploadAuthorized.Load(), s.metrics.uploadSessions.Load(),
		s.metrics.uploadRecords.Load(), s.metrics.uploadReplays.Load(),
		s.metrics.uploadFailures.Load(), s.metrics.uploadRejected.Load(), s.metrics.deleted.Load())
}

func (s *proxyServer) modifyResponse(resp *http.Response) error {
	record, _ := resp.Request.Context().Value(archiveContextKey{}).(*pendingArchive)
	resp.Header.Del(archiveUserIDHeader)
	resp.Header.Del(archiveAPIKeyIDHeader)
	if record == nil {
		return nil
	}

	record.responseStatus = resp.StatusCode
	record.clientRequestID = strings.TrimSpace(resp.Header.Get(clientRequestIDHeader))
	if record.clientRequestID != "" {
		record.requestID = "client:" + record.clientRequestID
	}
	contentType := strings.ToLower(resp.Header.Get("Content-Type"))
	record.eligible = resp.StatusCode >= 200 && resp.StatusCode < 300 &&
		strings.Contains(contentType, "text/event-stream") &&
		record.requestIsStream && record.protocol != protocolUnknown &&
		validUUID(record.clientRequestID)
	if !record.eligible {
		if resp.StatusCode >= 200 && resp.StatusCode < 300 &&
			strings.Contains(contentType, "text/event-stream") &&
			record.requestIsStream && record.protocol != protocolUnknown &&
			!validUUID(record.clientRequestID) {
			s.metrics.missingClientIDs.Add(1)
			record.discard("missing_or_invalid_client_request_id")
			return nil
		}
		record.discard("ineligible_response")
		return nil
	}
	resp.Body = &archiveResponseBody{ReadCloser: resp.Body, record: record}
	return nil
}

func (s *proxyServer) proxyError(w http.ResponseWriter, req *http.Request, _ error) {
	if record, _ := req.Context().Value(archiveContextKey{}).(*pendingArchive); record != nil {
		record.discard("upstream_error")
	}
	http.Error(w, "upstream unavailable", http.StatusBadGateway)
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value = strings.TrimSpace(value); value != "" {
			return value
		}
	}
	return ""
}

func validUUID(value string) bool {
	if len(value) != 36 || value[8] != '-' || value[13] != '-' || value[18] != '-' || value[23] != '-' {
		return false
	}
	for index, char := range value {
		if index == 8 || index == 13 || index == 18 || index == 23 {
			continue
		}
		if !((char >= '0' && char <= '9') || (char >= 'a' && char <= 'f') || (char >= 'A' && char <= 'F')) {
			return false
		}
	}
	return true
}

func shouldArchive(req *http.Request) bool {
	path := req.URL.Path
	if req.Method == http.MethodGet && strings.EqualFold(req.Header.Get("Upgrade"), "websocket") {
		return isResponsesPath(path)
	}
	if req.Method != http.MethodPost {
		return false
	}
	switch path {
	case "/v1/messages", "/v1/chat/completions", "/chat/completions",
		"/antigravity/v1/messages":
		return true
	}
	if isResponsesPath(path) {
		return true
	}
	return (strings.HasPrefix(path, "/v1beta/models/") ||
		strings.HasPrefix(path, "/antigravity/v1beta/models/")) &&
		strings.Contains(path, ":streamGenerateContent")
}

func isResponsesPath(path string) bool {
	for _, base := range []string{"/v1/responses", "/responses", "/backend-api/codex/responses"} {
		if path == base || strings.HasPrefix(path, base+"/") {
			return true
		}
	}
	return false
}

func healthcheck(cfg config) error {
	address := cfg.ListenAddr
	if strings.HasPrefix(address, ":") {
		address = "127.0.0.1" + address
	}
	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Get("http://" + address + "/_archive/health")
	if err != nil {
		return err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("health status %d", resp.StatusCode)
	}
	return nil
}

func main() {
	cfg, err := loadConfig()
	if err != nil {
		log.Fatal(err)
	}
	if len(os.Args) > 1 && os.Args[1] == "healthcheck" {
		if err := healthcheck(cfg); err != nil {
			log.Fatal(err)
		}
		return
	}
	store, err := newPostgresArchiveStore(cfg.DatabaseURL)
	if err != nil {
		log.Fatal(err)
	}
	defer func() { _ = store.Close() }()
	if handled, err := runTrajectoryCLI(context.Background(), cfg, store, os.Args[1:]); handled {
		if err != nil {
			log.Fatal(err)
		}
		return
	}

	uploadStore, err := newPostgresArchiveStore(cfg.TrajectoryUploadDatabaseURL)
	if err != nil {
		log.Fatal(err)
	}
	defer func() { _ = uploadStore.Close() }()
	serverHandler := newProxyServerWithUpload(cfg, store, uploadStore)
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if cfg.RetentionSweepEnabled {
		serverHandler.startRetentionWorker(ctx)
	} else {
		log.Printf(`{"event":"archive_retention_disabled","retention_days":30}`)
	}
	serverHandler.startIdentityResolver(ctx)
	serverHandler.startUploadCleanupWorker(ctx)
	serverHandler.startKeywordWorker(ctx)

	server := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           serverHandler,
		ReadHeaderTimeout: 15 * time.Second,
		IdleTimeout:       120 * time.Second,
	}
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdownCtx)
	}()
	log.Printf(`{"event":"archive_proxy_started","plugin_version":%q,"listen":%q,"upstream":%q,"retention_days":30,"retention_sweep_enabled":%t}`,
		archivePluginVersion, cfg.ListenAddr, cfg.UpstreamURL.String(), cfg.RetentionSweepEnabled)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}

func (s *proxyServer) wakeResolver() {
	select {
	case s.resolverWake <- struct{}{}:
	default:
	}
}

func (s *proxyServer) startIdentityResolver(ctx context.Context) {
	run := func() {
		opCtx, cancel := context.WithTimeout(ctx, s.cfg.DatabaseTimeout)
		stats, err := s.store.ResolveDue(opCtx, time.Now().UTC(), s.cfg.ResolveBatchSize)
		cancel()
		s.metrics.identityResolved.Add(stats.Resolved)
		s.metrics.identityRetried.Add(stats.Retried)
		s.metrics.identityTimeouts.Add(stats.TimedOut)
		s.metrics.identityAmbiguous.Add(stats.Ambiguous)
		s.metrics.inserted.Add(stats.Inserted)
		if err != nil {
			s.metrics.identityFailures.Add(1)
			log.Printf(`{"event":"archive_identity_resolver_failed","resolved":%d,"inserted":%d,"retried":%d,"timed_out":%d,"ambiguous":%d,"error":%q}`,
				stats.Resolved, stats.Inserted, stats.Retried, stats.TimedOut, stats.Ambiguous, err.Error())
			return
		}
		if stats.Resolved > 0 || stats.Retried > 0 || stats.TimedOut > 0 || stats.Ambiguous > 0 {
			log.Printf(`{"event":"archive_identity_resolver","resolved":%d,"inserted":%d,"retried":%d,"timed_out":%d,"ambiguous":%d}`,
				stats.Resolved, stats.Inserted, stats.Retried, stats.TimedOut, stats.Ambiguous)
		}
	}
	run()
	go func() {
		ticker := time.NewTicker(s.cfg.ResolveInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				run()
			case <-s.resolverWake:
				run()
			}
		}
	}()
}

func (s *proxyServer) startRetentionWorker(ctx context.Context) {
	run := func() {
		for {
			opCtx, cancel := context.WithTimeout(ctx, s.cfg.DatabaseTimeout)
			deleted, err := s.store.DeleteExpired(opCtx, time.Now().UTC(), s.cfg.SweepBatchSize)
			cancel()
			if err != nil {
				log.Printf(`{"event":"archive_retention_failed","error":%q}`, err.Error())
				return
			}
			s.metrics.deleted.Add(deleted)
			if deleted > 0 {
				log.Printf(`{"event":"archive_retention_deleted","count":%d}`, deleted)
			}
			if deleted < int64(s.cfg.SweepBatchSize) {
				return
			}
		}
	}
	run()
	go func() {
		ticker := time.NewTicker(s.cfg.SweepInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				run()
			}
		}
	}()
}
