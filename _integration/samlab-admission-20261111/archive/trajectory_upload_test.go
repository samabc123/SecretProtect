package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"
)

type memoryUploadItem struct {
	RequestHash string
	Status      string
	Action      string
	ErrorCode   string
}

type memoryUploadStore struct {
	mu            sync.Mutex
	device        uploadDeviceCreate
	deviceStatus  string
	approved      adminIdentity
	tokenHash     string
	session       uploadSessionState
	items         map[string]memoryUploadItem
	beginCalls    int
	imports       []trajectoryImportRecord
	importUserIDs []int64
	finishActions []string
	exchangeErr   error
	compatibility error
}

func (s *memoryUploadStore) CreateUploadDeviceCode(_ context.Context, item uploadDeviceCreate) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.device = item
	s.deviceStatus = "pending"
	if s.items == nil {
		s.items = map[string]memoryUploadItem{}
	}
	return nil
}

func (s *memoryUploadStore) ApproveUploadDeviceCode(_ context.Context, userCodeHash string, identity adminIdentity, now time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if userCodeHash != s.device.UserCodeHash {
		return errUploadDeviceNotFound
	}
	if !s.device.ExpiresAt.After(now) {
		return errUploadDeviceExpired
	}
	if s.deviceStatus == "consumed" {
		return errUploadDeviceConsumed
	}
	s.approved = identity
	s.deviceStatus = "approved"
	return nil
}

func (s *memoryUploadStore) ExchangeUploadDeviceCode(_ context.Context, item uploadDeviceExchange) (uploadSessionState, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.exchangeErr != nil {
		return uploadSessionState{}, s.exchangeErr
	}
	if item.DeviceCodeHash != s.device.DeviceCodeHash {
		return uploadSessionState{}, errUploadDeviceNotFound
	}
	if !s.device.ExpiresAt.After(item.Now) {
		return uploadSessionState{}, errUploadDeviceExpired
	}
	if s.deviceStatus == "pending" {
		return uploadSessionState{}, errUploadPending
	}
	if s.deviceStatus == "consumed" {
		return uploadSessionState{}, errUploadDeviceConsumed
	}
	s.deviceStatus = "consumed"
	s.tokenHash = item.TokenHash
	s.session = uploadSessionState{
		ID: item.SessionID, ImportBatchID: item.BatchID, UserID: s.approved.ID,
		Status: "active", MaxBytes: item.MaxBytes, CreatedAt: item.Now, ExpiresAt: item.ExpiresAt,
	}
	return s.session, nil
}

func (s *memoryUploadStore) BeginUploadItem(_ context.Context, item uploadItemBegin) (uploadItemReservation, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.beginCalls++
	if item.TokenHash != s.tokenHash {
		return uploadItemReservation{}, errUploadTokenInvalid
	}
	if s.session.Status != "active" {
		return uploadItemReservation{}, errUploadSessionClosed
	}
	if !s.session.ExpiresAt.After(item.Now) {
		return uploadItemReservation{}, errUploadSessionExpired
	}
	if existing, ok := s.items[item.TrajectoryKeyHash]; ok {
		if existing.RequestHash != item.RequestHash {
			return uploadItemReservation{}, errUploadReplayConflict
		}
		if existing.Status == "completed" {
			return uploadItemReservation{
				SessionID: s.session.ID, ImportBatchID: s.session.ImportBatchID,
				UserID: s.session.UserID, Replay: true, Action: existing.Action,
			}, nil
		}
		return uploadItemReservation{}, errUploadInProgress
	}
	if s.session.ReceivedBytes > s.session.MaxBytes-item.RequestBytes {
		return uploadItemReservation{}, errUploadLimitExceeded
	}
	s.items[item.TrajectoryKeyHash] = memoryUploadItem{RequestHash: item.RequestHash, Status: "processing"}
	s.session.ReceivedBytes += item.RequestBytes
	s.session.Stats.Total++
	return uploadItemReservation{SessionID: s.session.ID, ImportBatchID: s.session.ImportBatchID, UserID: s.session.UserID}, nil
}

func (s *memoryUploadStore) FinishUploadItem(_ context.Context, _ string, trajectoryKeyHash, action, errorCode string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	item := s.items[trajectoryKeyHash]
	item.Action = action
	item.ErrorCode = errorCode
	item.Status = "completed"
	if action == "failed" {
		item.Status = "failed"
		s.session.Stats.Failed++
	} else if action == "inserted" {
		s.session.Stats.Inserted++
	} else if action == "updated" {
		s.session.Stats.Updated++
	} else {
		s.session.Stats.Skipped++
	}
	s.items[trajectoryKeyHash] = item
	s.finishActions = append(s.finishActions, action+":"+errorCode)
	return nil
}

func (s *memoryUploadStore) GetUploadSession(_ context.Context, tokenHash string, now time.Time) (uploadSessionState, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if tokenHash != s.tokenHash {
		return uploadSessionState{}, errUploadTokenInvalid
	}
	if !s.session.ExpiresAt.After(now) {
		return uploadSessionState{}, errUploadSessionExpired
	}
	if s.session.Status != "active" {
		return uploadSessionState{}, errUploadSessionClosed
	}
	return s.session, nil
}

func (s *memoryUploadStore) CompleteUploadSession(_ context.Context, tokenHash string, now time.Time) (uploadSessionState, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if tokenHash != s.tokenHash {
		return uploadSessionState{}, errUploadTokenInvalid
	}
	if s.session.Status != "active" {
		return uploadSessionState{}, errUploadSessionClosed
	}
	for _, item := range s.items {
		if item.Status == "processing" {
			return uploadSessionState{}, errUploadHasProcessing
		}
	}
	s.session.Status = "completed"
	s.session.CompletedAt = &now
	return s.session, nil
}

func (s *memoryUploadStore) CleanupUploadSessions(context.Context, time.Time) (int64, error) {
	return 0, nil
}

func (s *memoryUploadStore) CheckTrajectoryUploadCompatibility(context.Context) error {
	return s.compatibility
}

func (s *memoryUploadStore) ImportTrajectory(_ context.Context, _ string, userID int64, record trajectoryImportRecord) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.imports = append(s.imports, record)
	s.importUserIDs = append(s.importUserIDs, userID)
	return "inserted", nil
}

func uploadTestConfig(upstream, privacy *httptest.Server) config {
	cfg := proxyTestConfig(upstream, privacy)
	cfg.UploadDeviceTTL = 10 * time.Minute
	cfg.UploadSessionTTL = 2 * time.Hour
	cfg.UploadPollInterval = 3 * time.Second
	cfg.UploadMaxBytes = 64 << 20
	cfg.UploadMaxRecordBytes = maxTrajectoryRecordBytes
	return cfg
}

func newUploadAuthServer(t *testing.T) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.URL.Path != "/api/v1/auth/me" {
			http.NotFound(w, req)
			return
		}
		identity := `{"id":42,"username":"user","email":"user@example.test","role":"user","status":"active"}`
		switch req.Header.Get("Authorization") {
		case "Bearer active-user":
		case "Bearer inactive-user":
			identity = `{"id":43,"username":"disabled","email":"disabled@example.test","role":"user","status":"disabled"}`
		case "Bearer admin-user":
			identity = `{"id":1,"username":"admin","email":"admin@example.test","role":"admin","status":"active"}`
		default:
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"code":0,"message":"success","data":`+identity+`}`)
	}))
}

func decodeAPIData(t *testing.T, response *http.Response) map[string]any {
	t.Helper()
	defer func() { _ = response.Body.Close() }()
	var envelope struct {
		Code    int            `json:"code"`
		Message string         `json:"message"`
		Data    map[string]any `json:"data"`
	}
	if err := json.NewDecoder(response.Body).Decode(&envelope); err != nil {
		t.Fatal(err)
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 || envelope.Code != 0 {
		t.Fatalf("status=%d code=%d message=%s", response.StatusCode, envelope.Code, envelope.Message)
	}
	return envelope.Data
}

func startAndAuthorizeUpload(t *testing.T, proxyURL string) (string, string) {
	t.Helper()
	response, err := http.Post(proxyURL+"/_archive/api/v1/trajectory-sync/device/start", "application/json", strings.NewReader(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	started := decodeAPIData(t, response)
	userCode := started["user_code"].(string)
	deviceCode := started["device_code"].(string)
	req, _ := http.NewRequest(http.MethodPost, proxyURL+"/_archive/api/v1/trajectory-sync/device/approve", strings.NewReader(fmt.Sprintf(`{"user_code":%q}`, userCode)))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer active-user")
	response, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = decodeAPIData(t, response)
	response, err = http.Post(proxyURL+"/_archive/api/v1/trajectory-sync/device/token", "application/json", strings.NewReader(fmt.Sprintf(`{"device_code":%q}`, deviceCode)))
	if err != nil {
		t.Fatal(err)
	}
	exchanged := decodeAPIData(t, response)
	return exchanged["upload_token"].(string), userCode
}

func TestTrajectoryHTTPSUploadBindsActiveUserRedactsTwiceAndRejectsReplayMutation(t *testing.T) {
	auth := newUploadAuthServer(t)
	defer auth.Close()
	privacyCalls := 0
	privacy := newPrivacyTestServer(t, func(text string) (string, bool, int) {
		privacyCalls++
		redacted := strings.ReplaceAll(text, "PII-SECRET", "[REDACTED]")
		return redacted, redacted != text, map[bool]int{true: 1, false: 0}[redacted != text]
	})
	defer privacy.Close()
	uploadStore := &memoryUploadStore{}
	server := httptest.NewServer(newProxyServerWithUpload(uploadTestConfig(auth, privacy), &memoryArchiveStore{}, uploadStore))
	defer server.Close()
	uploadToken, _ := startAndAuthorizeUpload(t, server.URL)
	record := validTrajectoryRecord(time.Now().UTC())
	record.Messages[0].Content = json.RawMessage(`"password=PII-SECRET"`)
	raw, _ := json.Marshal(record)
	req, _ := http.NewRequest(http.MethodPost, server.URL+"/_archive/api/v1/trajectory-sync/trajectories", strings.NewReader(string(raw)))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(uploadTokenHeader, uploadToken)
	req.Header.Set(archiveUserIDHeader, "999")
	req.Header.Set(archiveAPIKeyIDHeader, "888")
	response, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	result := decodeAPIData(t, response)
	if result["action"] != "inserted" || result["replayed"] != false {
		t.Fatalf("unexpected upload result: %+v", result)
	}
	if len(uploadStore.imports) != 1 || uploadStore.importUserIDs[0] != 42 {
		t.Fatalf("imports=%d user_ids=%v", len(uploadStore.imports), uploadStore.importUserIDs)
	}
	persisted, _ := json.Marshal(uploadStore.imports[0])
	if strings.Contains(string(persisted), "PII-SECRET") || !strings.Contains(string(persisted), "[REDACTED]") {
		t.Fatalf("PII redaction failed: %s", persisted)
	}
	if uploadStore.imports[0].Messages[0].Extra.PrivacyEntities == 0 {
		t.Fatal("first-pass event privacy count was lost during second-pass filtering")
	}
	if privacyCalls < 2 {
		t.Fatalf("privacy filter calls=%d, want at least two passes", privacyCalls)
	}
	if strings.Contains(fmt.Sprintf("%+v", uploadStore.items), "PII-SECRET") {
		t.Fatal("raw PII entered upload item state")
	}

	req, _ = http.NewRequest(http.MethodPost, server.URL+"/_archive/api/v1/trajectory-sync/trajectories", strings.NewReader(string(raw)))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(uploadTokenHeader, uploadToken)
	response, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	result = decodeAPIData(t, response)
	if result["replayed"] != true || len(uploadStore.imports) != 1 {
		t.Fatalf("replay result=%+v imports=%d", result, len(uploadStore.imports))
	}

	record.Title = "changed"
	changed, _ := json.Marshal(record)
	req, _ = http.NewRequest(http.MethodPost, server.URL+"/_archive/api/v1/trajectory-sync/trajectories", strings.NewReader(string(changed)))
	req.Header.Set(uploadTokenHeader, uploadToken)
	response, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if response.StatusCode != http.StatusConflict {
		t.Fatalf("mutated replay status=%d", response.StatusCode)
	}

	req, _ = http.NewRequest(http.MethodPost, server.URL+"/_archive/api/v1/trajectory-sync/complete", strings.NewReader(`{}`))
	req.Header.Set(uploadTokenHeader, uploadToken)
	response, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = decodeAPIData(t, response)
	req, _ = http.NewRequest(http.MethodGet, server.URL+"/_archive/api/v1/trajectory-sync/session", nil)
	req.Header.Set(uploadTokenHeader, uploadToken)
	response, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if response.StatusCode != http.StatusGone {
		t.Fatalf("completed token status=%d", response.StatusCode)
	}
}

func TestTrajectoryHTTPSUploadCanonicalizesLegacyTerminalBeforeFinalAnswer(t *testing.T) {
	auth := newUploadAuthServer(t)
	defer auth.Close()
	privacy := newPrivacyTestServer(t, func(text string) (string, bool, int) { return text, false, 0 })
	defer privacy.Close()
	uploadStore := &memoryUploadStore{}
	server := httptest.NewServer(newProxyServerWithUpload(uploadTestConfig(auth, privacy), &memoryArchiveStore{}, uploadStore))
	defer server.Close()
	uploadToken, _ := startAndAuthorizeUpload(t, server.URL)

	record := validTrajectoryRecord(time.Now().UTC())
	terminal := record.Messages[2]
	finalAnswer := record.Messages[1]
	record.Messages[1] = terminal
	record.Messages[2] = finalAnswer
	for index := range record.Messages {
		record.Messages[index].Sequence = index + 1
	}
	raw, _ := json.Marshal(record)
	req, _ := http.NewRequest(http.MethodPost, server.URL+"/_archive/api/v1/trajectory-sync/trajectories", strings.NewReader(string(raw)))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(uploadTokenHeader, uploadToken)
	response, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	result := decodeAPIData(t, response)
	if result["action"] != "inserted" || len(uploadStore.imports) != 1 {
		t.Fatalf("result=%+v imports=%d", result, len(uploadStore.imports))
	}
	persisted := uploadStore.imports[0]
	last := persisted.Messages[len(persisted.Messages)-1]
	if last.Extra.EventType != "turn_completed" || last.Sequence != len(persisted.Messages) {
		t.Fatalf("terminal was not canonicalized: %+v", persisted.Messages)
	}
	if persisted.FinalText != "complete answer" {
		t.Fatalf("final_text=%q", persisted.FinalText)
	}
}

func TestTrajectoryHTTPSUploadAcceptsLegacyClientExtraMetadata(t *testing.T) {
	auth := newUploadAuthServer(t)
	defer auth.Close()
	privacy := newPrivacyTestServer(t, func(text string) (string, bool, int) { return text, false, 0 })
	defer privacy.Close()
	uploadStore := &memoryUploadStore{}
	server := httptest.NewServer(newProxyServerWithUpload(uploadTestConfig(auth, privacy), &memoryArchiveStore{}, uploadStore))
	defer server.Close()
	uploadToken, _ := startAndAuthorizeUpload(t, server.URL)

	record := validTrajectoryRecord(time.Now().UTC())
	raw, _ := json.Marshal(record)
	var legacy map[string]any
	if err := json.Unmarshal(raw, &legacy); err != nil {
		t.Fatal(err)
	}
	messages := legacy["messages"].([]any)
	extra := messages[0].(map[string]any)["extra"].(map[string]any)
	extra["source"] = "event_msg"
	extra["kind"] = "plan"
	raw, _ = json.Marshal(legacy)

	req, _ := http.NewRequest(http.MethodPost, server.URL+"/_archive/api/v1/trajectory-sync/trajectories", bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(uploadTokenHeader, uploadToken)
	response, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	result := decodeAPIData(t, response)
	if result["action"] != "inserted" || len(uploadStore.imports) != 1 {
		t.Fatalf("result=%+v imports=%d", result, len(uploadStore.imports))
	}
	persisted, _ := json.Marshal(uploadStore.imports[0])
	var canonical map[string]any
	if err := json.Unmarshal(persisted, &canonical); err != nil {
		t.Fatal(err)
	}
	persistedExtra := canonical["messages"].([]any)[0].(map[string]any)["extra"].(map[string]any)
	if _, ok := persistedExtra["source"]; ok {
		t.Fatalf("legacy source metadata was persisted: %s", persisted)
	}
	if _, ok := persistedExtra["kind"]; ok {
		t.Fatalf("legacy kind metadata was persisted: %s", persisted)
	}
}

func TestTrajectoryDeviceApprovalAllowsActiveUserButNotInactiveOrAdminAPI(t *testing.T) {
	auth := newUploadAuthServer(t)
	defer auth.Close()
	privacy := newPrivacyTestServer(t, func(text string) (string, bool, int) { return text, false, 0 })
	defer privacy.Close()
	uploadStore := &memoryUploadStore{}
	server := httptest.NewServer(newProxyServerWithUpload(uploadTestConfig(auth, privacy), &memoryArchiveStore{}, uploadStore))
	defer server.Close()
	response, err := http.Post(server.URL+"/_archive/api/v1/trajectory-sync/device/start", "application/json", strings.NewReader(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	started := decodeAPIData(t, response)
	body := fmt.Sprintf(`{"user_code":%q}`, started["user_code"])
	req, _ := http.NewRequest(http.MethodPost, server.URL+"/_archive/api/v1/trajectory-sync/device/approve", strings.NewReader(body))
	req.Header.Set("Authorization", "Bearer inactive-user")
	response, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if response.StatusCode != http.StatusForbidden {
		t.Fatalf("inactive approval status=%d", response.StatusCode)
	}
	req, _ = http.NewRequest(http.MethodPost, server.URL+"/_archive/api/v1/trajectory-sync/device/approve", strings.NewReader(strings.TrimSuffix(body, "}")+`,"email":"forged@example.test"}`))
	req.Header.Set("Authorization", "Bearer active-user")
	response, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if response.StatusCode != http.StatusBadRequest {
		t.Fatalf("forged target email status=%d", response.StatusCode)
	}
	req, _ = http.NewRequest(http.MethodGet, server.URL+"/_archive/api/v1/conversation-archives", nil)
	req.Header.Set("Authorization", "Bearer active-user")
	response, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if response.StatusCode != http.StatusForbidden {
		t.Fatalf("non-admin archive query status=%d", response.StatusCode)
	}
}

func TestTrajectoryDeviceTokenReportsPendingSlowDownAndExpiry(t *testing.T) {
	auth := newUploadAuthServer(t)
	defer auth.Close()
	privacy := newPrivacyTestServer(t, func(text string) (string, bool, int) { return text, false, 0 })
	defer privacy.Close()
	uploadStore := &memoryUploadStore{}
	server := httptest.NewServer(newProxyServerWithUpload(uploadTestConfig(auth, privacy), &memoryArchiveStore{}, uploadStore))
	defer server.Close()
	response, err := http.Post(server.URL+"/_archive/api/v1/trajectory-sync/device/start", "application/json", strings.NewReader(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	started := decodeAPIData(t, response)
	body := fmt.Sprintf(`{"device_code":%q}`, started["device_code"])
	response, err = http.Post(server.URL+"/_archive/api/v1/trajectory-sync/device/token", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if response.StatusCode != http.StatusPreconditionRequired {
		t.Fatalf("pending status=%d", response.StatusCode)
	}
	uploadStore.exchangeErr = errUploadSlowDown
	response, err = http.Post(server.URL+"/_archive/api/v1/trajectory-sync/device/token", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if response.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("slow-down status=%d", response.StatusCode)
	}
	uploadStore.exchangeErr = nil
	uploadStore.device.ExpiresAt = time.Now().Add(-time.Second)
	response, err = http.Post(server.URL+"/_archive/api/v1/trajectory-sync/device/token", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if response.StatusCode != http.StatusGone {
		t.Fatalf("expired status=%d", response.StatusCode)
	}
}

type failingUploadReader struct{}

func (failingUploadReader) Read([]byte) (int, error) { return 0, errors.New("synthetic disconnect") }

func TestTrajectoryUploadRejectsInvalidInterruptedAndOversizedBodiesBeforeReservation(t *testing.T) {
	auth := newUploadAuthServer(t)
	defer auth.Close()
	privacy := newPrivacyTestServer(t, func(text string) (string, bool, int) { return text, false, 0 })
	defer privacy.Close()
	uploadStore := &memoryUploadStore{
		tokenHash: hashSecret("token-that-is-long-enough-for-test"),
		session:   uploadSessionState{ID: "00000000-0000-4000-8000-000000000001", ImportBatchID: "00000000-0000-4000-8000-000000000002", UserID: 42, Status: "active", MaxBytes: 1 << 20, ExpiresAt: time.Now().Add(time.Hour)},
		items:     map[string]memoryUploadItem{},
	}
	cfg := uploadTestConfig(auth, privacy)
	cfg.UploadMaxRecordBytes = 512
	serverHandler := newProxyServerWithUpload(cfg, &memoryArchiveStore{}, uploadStore)
	server := httptest.NewServer(serverHandler)
	defer server.Close()
	for _, body := range []string{"{", strings.Repeat("x", 513)} {
		req, _ := http.NewRequest(http.MethodPost, server.URL+"/_archive/api/v1/trajectory-sync/trajectories", strings.NewReader(body))
		req.Header.Set(uploadTokenHeader, "token-that-is-long-enough-for-test")
		response, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		_ = response.Body.Close()
		if response.StatusCode != http.StatusBadRequest && response.StatusCode != http.StatusRequestEntityTooLarge {
			t.Fatalf("body length=%d status=%d", len(body), response.StatusCode)
		}
	}
	recorder := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/_archive/api/v1/trajectory-sync/trajectories", nil)
	req.Body = io.NopCloser(failingUploadReader{})
	req.Header.Set(uploadTokenHeader, "token-that-is-long-enough-for-test")
	serverHandler.ServeHTTP(recorder, req)
	if recorder.Code != http.StatusBadRequest {
		t.Fatalf("interrupted status=%d", recorder.Code)
	}
	if uploadStore.beginCalls != 0 || len(uploadStore.imports) != 0 || len(uploadStore.items) != 0 {
		t.Fatalf("invalid requests reserved state: begin=%d imports=%d items=%d", uploadStore.beginCalls, len(uploadStore.imports), len(uploadStore.items))
	}
}

func TestTrajectoryUploadRejectsExpiredAndSessionByteLimit(t *testing.T) {
	auth := newUploadAuthServer(t)
	defer auth.Close()
	privacy := newPrivacyTestServer(t, func(text string) (string, bool, int) { return text, false, 0 })
	defer privacy.Close()
	token := "token-that-is-long-enough-for-limit-test"
	uploadStore := &memoryUploadStore{
		tokenHash: hashSecret(token),
		session:   uploadSessionState{ID: "00000000-0000-4000-8000-000000000021", ImportBatchID: "00000000-0000-4000-8000-000000000022", UserID: 42, Status: "active", MaxBytes: 1, ExpiresAt: time.Now().Add(time.Hour)},
		items:     map[string]memoryUploadItem{},
	}
	server := httptest.NewServer(newProxyServerWithUpload(uploadTestConfig(auth, privacy), &memoryArchiveStore{}, uploadStore))
	defer server.Close()
	record := validTrajectoryRecord(time.Now().UTC())
	raw, _ := json.Marshal(record)
	req, _ := http.NewRequest(http.MethodPost, server.URL+"/_archive/api/v1/trajectory-sync/trajectories", strings.NewReader(string(raw)))
	req.Header.Set(uploadTokenHeader, token)
	response, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if response.StatusCode != http.StatusRequestEntityTooLarge || len(uploadStore.imports) != 0 {
		t.Fatalf("byte limit status=%d imports=%d", response.StatusCode, len(uploadStore.imports))
	}
	uploadStore.session.MaxBytes = 64 << 20
	uploadStore.session.ExpiresAt = time.Now().Add(-time.Second)
	req, _ = http.NewRequest(http.MethodPost, server.URL+"/_archive/api/v1/trajectory-sync/trajectories", strings.NewReader(string(raw)))
	req.Header.Set(uploadTokenHeader, token)
	response, err = http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if response.StatusCode != http.StatusGone || len(uploadStore.imports) != 0 {
		t.Fatalf("expired session status=%d imports=%d", response.StatusCode, len(uploadStore.imports))
	}
}

func TestTrajectoryUploadPrivacyFailureNeverImports(t *testing.T) {
	auth := newUploadAuthServer(t)
	defer auth.Close()
	privacy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "synthetic failure", http.StatusServiceUnavailable)
	}))
	defer privacy.Close()
	token := "token-that-is-long-enough-for-privacy-test"
	uploadStore := &memoryUploadStore{
		tokenHash: hashSecret(token),
		session:   uploadSessionState{ID: "00000000-0000-4000-8000-000000000011", ImportBatchID: "00000000-0000-4000-8000-000000000012", UserID: 42, Status: "active", MaxBytes: 64 << 20, ExpiresAt: time.Now().Add(time.Hour)},
		items:     map[string]memoryUploadItem{},
	}
	server := httptest.NewServer(newProxyServerWithUpload(uploadTestConfig(auth, privacy), &memoryArchiveStore{}, uploadStore))
	defer server.Close()
	record := validTrajectoryRecord(time.Now().UTC())
	record.Messages[0].Content = json.RawMessage(`"PII-SECRET"`)
	raw, _ := json.Marshal(record)
	req, _ := http.NewRequest(http.MethodPost, server.URL+"/_archive/api/v1/trajectory-sync/trajectories", strings.NewReader(string(raw)))
	req.Header.Set(uploadTokenHeader, token)
	response, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if response.StatusCode != http.StatusServiceUnavailable || len(uploadStore.imports) != 0 {
		t.Fatalf("status=%d imports=%d", response.StatusCode, len(uploadStore.imports))
	}
	if len(uploadStore.finishActions) != 1 || uploadStore.finishActions[0] != "failed:privacy_filter_failed" {
		t.Fatalf("finish actions=%v", uploadStore.finishActions)
	}
	if strings.Contains(fmt.Sprintf("%+v", uploadStore.items), "PII-SECRET") {
		t.Fatal("PII entered upload state after privacy failure")
	}
}

func TestTrajectoryHTTPSUploadMigrationAndDeploymentContract(t *testing.T) {
	migration, err := os.ReadFile("migrations/003_trajectory_https_upload.sql")
	if err != nil {
		t.Fatal(err)
	}
	sqlText := string(migration)
	for _, required := range []string{
		"trajectory_upload_device_codes", "trajectory_upload_sessions", "trajectory_upload_items",
		"device_code_hash CHAR(64)", "token_hash CHAR(64)", "request_sha256 CHAR(64)",
		"DROP COLUMN IF EXISTS email_snapshot", "DROP COLUMN IF EXISTS role_snapshot",
	} {
		if !strings.Contains(sqlText, required) {
			t.Fatalf("upload migration missing %q", required)
		}
	}
	uploadSource, err := os.ReadFile("trajectory_upload.go")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Count(string(uploadSource), "$1::timestamptz - INTERVAL") < 3 {
		t.Fatal("upload cleanup timestamps are not explicitly typed")
	}
	deploy, err := os.ReadFile("deploy/deploy_external_plugin.sh")
	if err != nil {
		t.Fatal(err)
	}
	deployText := string(deploy)
	for _, required := range []string{
		"TRAJECTORY_UPLOAD_DATABASE_URL", "ARCHIVE_PUBLIC_URL", "UPLOAD_DEVICE_TTL",
		"GRANT SELECT ON TABLE conversation_archive_plugin_migrations TO trajectory_importer",
		`"plugin_version":"external-v3"`,
	} {
		if !strings.Contains(deployText, required) {
			t.Fatalf("upload deployment missing %q", required)
		}
	}
}
