package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

const uploadTokenHeader = "X-Archive-Upload-Token"

var (
	errUploadDeviceNotFound = errors.New("upload device code not found")
	errUploadDeviceExpired  = errors.New("upload device code expired")
	errUploadDeviceConsumed = errors.New("upload device code consumed")
	errUploadPending        = errors.New("upload authorization pending")
	errUploadSlowDown       = errors.New("upload polling too quickly")
	errUploadTokenInvalid   = errors.New("upload token invalid")
	errUploadSessionExpired = errors.New("upload session expired")
	errUploadSessionClosed  = errors.New("upload session closed")
	errUploadLimitExceeded  = errors.New("upload session size limit exceeded")
	errUploadReplayConflict = errors.New("trajectory replay conflict")
	errUploadInProgress     = errors.New("trajectory upload already in progress")
	errUploadHasProcessing  = errors.New("upload session still has processing records")
)

type trajectoryUploadStore interface {
	CreateUploadDeviceCode(context.Context, uploadDeviceCreate) error
	ApproveUploadDeviceCode(context.Context, string, adminIdentity, time.Time) error
	ExchangeUploadDeviceCode(context.Context, uploadDeviceExchange) (uploadSessionState, error)
	BeginUploadItem(context.Context, uploadItemBegin) (uploadItemReservation, error)
	FinishUploadItem(context.Context, string, string, string, string) error
	GetUploadSession(context.Context, string, time.Time) (uploadSessionState, error)
	CompleteUploadSession(context.Context, string, time.Time) (uploadSessionState, error)
	CleanupUploadSessions(context.Context, time.Time) (int64, error)
	CheckTrajectoryUploadCompatibility(context.Context) error
	ImportTrajectory(context.Context, string, int64, trajectoryImportRecord) (string, error)
}

type uploadDeviceCreate struct {
	ID                  string
	DeviceCodeHash      string
	UserCodeHash        string
	PollIntervalSeconds int
	ExpiresAt           time.Time
}

type uploadDeviceExchange struct {
	DeviceCodeHash string
	SessionID      string
	BatchID        string
	TokenHash      string
	ManifestHash   string
	Now            time.Time
	ExpiresAt      time.Time
	MaxBytes       int64
}

type uploadSessionState struct {
	ID            string                `json:"session_id"`
	ImportBatchID string                `json:"batch_id"`
	UserID        int64                 `json:"user_id"`
	Status        string                `json:"status"`
	MaxBytes      int64                 `json:"max_bytes"`
	ReceivedBytes int64                 `json:"received_bytes"`
	Stats         trajectoryImportStats `json:"stats"`
	CreatedAt     time.Time             `json:"created_at"`
	ExpiresAt     time.Time             `json:"expires_at"`
	CompletedAt   *time.Time            `json:"completed_at,omitempty"`
}

type uploadItemBegin struct {
	TokenHash         string
	TrajectoryKeyHash string
	RequestHash       string
	RequestBytes      int64
	Now               time.Time
}

type uploadItemReservation struct {
	SessionID     string
	ImportBatchID string
	UserID        int64
	Replay        bool
	Action        string
}

func (s *postgresArchiveStore) CreateUploadDeviceCode(ctx context.Context, item uploadDeviceCreate) error {
	_, err := s.db.ExecContext(ctx, `
INSERT INTO trajectory_upload_device_codes (
  id, device_code_hash, user_code_hash, status, poll_interval_seconds, expires_at
) VALUES ($1::uuid, $2, $3, 'pending', $4, $5)`,
		item.ID, item.DeviceCodeHash, item.UserCodeHash, item.PollIntervalSeconds, item.ExpiresAt)
	return err
}

func (s *postgresArchiveStore) ApproveUploadDeviceCode(ctx context.Context, userCodeHash string, identity adminIdentity, now time.Time) error {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	var id, status string
	var expiresAt time.Time
	var existingUserID sql.NullInt64
	err = tx.QueryRowContext(ctx, `
SELECT id::text, status, expires_at, user_id
FROM trajectory_upload_device_codes
WHERE user_code_hash = $1
FOR UPDATE`, userCodeHash).Scan(&id, &status, &expiresAt, &existingUserID)
	if errors.Is(err, sql.ErrNoRows) {
		return errUploadDeviceNotFound
	}
	if err != nil {
		return err
	}
	if !expiresAt.After(now) {
		_, _ = tx.ExecContext(ctx, `UPDATE trajectory_upload_device_codes SET status = 'expired' WHERE id = $1::uuid`, id)
		if commitErr := tx.Commit(); commitErr != nil {
			return commitErr
		}
		return errUploadDeviceExpired
	}
	switch status {
	case "pending":
		_, err = tx.ExecContext(ctx, `
UPDATE trajectory_upload_device_codes
SET status = 'approved', user_id = $2, approved_at = $3
WHERE id = $1::uuid`, id, identity.ID, now)
		if err != nil {
			return err
		}
		return tx.Commit()
	case "approved":
		if existingUserID.Valid && existingUserID.Int64 == identity.ID {
			return tx.Commit()
		}
		return fmt.Errorf("device code was approved by another account")
	case "expired":
		return errUploadDeviceExpired
	default:
		return errUploadDeviceConsumed
	}
}

func (s *postgresArchiveStore) ExchangeUploadDeviceCode(ctx context.Context, item uploadDeviceExchange) (uploadSessionState, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return uploadSessionState{}, err
	}
	defer func() { _ = tx.Rollback() }()
	var deviceID, status string
	var userID sql.NullInt64
	var expiresAt time.Time
	var createdAt time.Time
	var lastPollAt sql.NullTime
	var pollInterval int
	err = tx.QueryRowContext(ctx, `
SELECT id::text, status, user_id, expires_at, last_poll_at, poll_interval_seconds, created_at
FROM trajectory_upload_device_codes
WHERE device_code_hash = $1
FOR UPDATE`, item.DeviceCodeHash).Scan(
		&deviceID, &status, &userID, &expiresAt, &lastPollAt, &pollInterval, &createdAt)
	if errors.Is(err, sql.ErrNoRows) {
		return uploadSessionState{}, errUploadDeviceNotFound
	}
	if err != nil {
		return uploadSessionState{}, err
	}
	if !expiresAt.After(item.Now) {
		_, _ = tx.ExecContext(ctx, `UPDATE trajectory_upload_device_codes SET status = 'expired' WHERE id = $1::uuid`, deviceID)
		if commitErr := tx.Commit(); commitErr != nil {
			return uploadSessionState{}, commitErr
		}
		return uploadSessionState{}, errUploadDeviceExpired
	}
	if status == "pending" {
		if lastPollAt.Valid && item.Now.Sub(lastPollAt.Time) < time.Duration(pollInterval)*time.Second {
			if commitErr := tx.Commit(); commitErr != nil {
				return uploadSessionState{}, commitErr
			}
			return uploadSessionState{}, errUploadSlowDown
		}
		_, err = tx.ExecContext(ctx, `UPDATE trajectory_upload_device_codes SET last_poll_at = $2 WHERE id = $1::uuid`, deviceID, item.Now)
		if err != nil {
			return uploadSessionState{}, err
		}
		if err := tx.Commit(); err != nil {
			return uploadSessionState{}, err
		}
		return uploadSessionState{}, errUploadPending
	}
	if status == "expired" {
		return uploadSessionState{}, errUploadDeviceExpired
	}
	if status == "consumed" {
		return uploadSessionState{}, errUploadDeviceConsumed
	}
	if status != "approved" || !userID.Valid || userID.Int64 <= 0 {
		return uploadSessionState{}, errUploadPending
	}
	if err := checkLabUploadGrant(ctx, tx, userID.Int64, createdAt); err != nil {
		return uploadSessionState{}, err
	}
	identityLabel := fmt.Sprintf("user_id:%d", userID.Int64)
	_, err = tx.ExecContext(ctx, `
INSERT INTO agent_trajectory_import_batches (
  id, user_id, email_snapshot, source, schema_version, manifest_sha256, status
) VALUES ($1::uuid, $2, $3, 'codex_https', $4, $5, 'importing')`,
		item.BatchID, userID.Int64, identityLabel, trajectorySchemaVersion, item.ManifestHash)
	if err != nil {
		return uploadSessionState{}, err
	}
	_, err = tx.ExecContext(ctx, `
INSERT INTO trajectory_upload_sessions (
  id, device_code_id, import_batch_id, user_id, token_hash, status,
  max_bytes, created_at, expires_at, last_seen_at
) VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, 'active', $6, $7, $8, $7)`,
		item.SessionID, deviceID, item.BatchID, userID.Int64, item.TokenHash,
		item.MaxBytes, item.Now, item.ExpiresAt)
	if err != nil {
		return uploadSessionState{}, err
	}
	_, err = tx.ExecContext(ctx, `
UPDATE trajectory_upload_device_codes
SET status = 'consumed', consumed_at = $2, last_poll_at = $2
WHERE id = $1::uuid`, deviceID, item.Now)
	if err != nil {
		return uploadSessionState{}, err
	}
	if err := tx.Commit(); err != nil {
		return uploadSessionState{}, err
	}
	return uploadSessionState{
		ID: item.SessionID, ImportBatchID: item.BatchID, UserID: userID.Int64,
		Status: "active", MaxBytes: item.MaxBytes, CreatedAt: item.Now, ExpiresAt: item.ExpiresAt,
	}, nil
}

func (s *postgresArchiveStore) BeginUploadItem(ctx context.Context, item uploadItemBegin) (uploadItemReservation, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return uploadItemReservation{}, err
	}
	defer func() { _ = tx.Rollback() }()
	state, err := lockUploadSession(ctx, tx, item.TokenHash, item.Now)
	if err != nil {
		return uploadItemReservation{}, err
	}
	var status, requestHash string
	var action sql.NullString
	err = tx.QueryRowContext(ctx, `
SELECT status, request_sha256, action
FROM trajectory_upload_items
WHERE session_id = $1::uuid AND trajectory_id = $2
FOR UPDATE`, state.ID, item.TrajectoryKeyHash).Scan(&status, &requestHash, &action)
	if err == nil {
		if requestHash != item.RequestHash {
			return uploadItemReservation{}, errUploadReplayConflict
		}
		if status == "completed" {
			if err := tx.Commit(); err != nil {
				return uploadItemReservation{}, err
			}
			return uploadItemReservation{SessionID: state.ID, ImportBatchID: state.ImportBatchID, UserID: state.UserID, Replay: true, Action: action.String}, nil
		}
		if status == "processing" {
			return uploadItemReservation{}, errUploadInProgress
		}
		_, err = tx.ExecContext(ctx, `
UPDATE trajectory_upload_items
SET status = 'processing', action = NULL, error_code = NULL, updated_at = $3
WHERE session_id = $1::uuid AND trajectory_id = $2`, state.ID, item.TrajectoryKeyHash, item.Now)
		if err != nil {
			return uploadItemReservation{}, err
		}
		_, err = tx.ExecContext(ctx, `
UPDATE trajectory_upload_sessions
SET failed_records = GREATEST(failed_records - 1, 0), last_seen_at = $2
WHERE id = $1::uuid`, state.ID, item.Now)
		if err != nil {
			return uploadItemReservation{}, err
		}
		if err := tx.Commit(); err != nil {
			return uploadItemReservation{}, err
		}
		return uploadItemReservation{SessionID: state.ID, ImportBatchID: state.ImportBatchID, UserID: state.UserID}, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return uploadItemReservation{}, err
	}
	if item.RequestBytes <= 0 || state.ReceivedBytes > state.MaxBytes-item.RequestBytes {
		return uploadItemReservation{}, errUploadLimitExceeded
	}
	_, err = tx.ExecContext(ctx, `
INSERT INTO trajectory_upload_items (
  session_id, trajectory_id, request_sha256, request_bytes, status, created_at, updated_at
) VALUES ($1::uuid, $2, $3, $4, 'processing', $5, $5)`,
		state.ID, item.TrajectoryKeyHash, item.RequestHash, item.RequestBytes, item.Now)
	if err != nil {
		return uploadItemReservation{}, err
	}
	_, err = tx.ExecContext(ctx, `
UPDATE trajectory_upload_sessions
SET received_bytes = received_bytes + $2, total_records = total_records + 1, last_seen_at = $3
WHERE id = $1::uuid`, state.ID, item.RequestBytes, item.Now)
	if err != nil {
		return uploadItemReservation{}, err
	}
	if err := tx.Commit(); err != nil {
		return uploadItemReservation{}, err
	}
	return uploadItemReservation{SessionID: state.ID, ImportBatchID: state.ImportBatchID, UserID: state.UserID}, nil
}

func (s *postgresArchiveStore) FinishUploadItem(ctx context.Context, sessionID, trajectoryKeyHash, action, errorCode string) error {
	if action != "inserted" && action != "updated" && action != "skipped" && action != "failed" {
		return fmt.Errorf("invalid upload item action")
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback() }()
	result, err := tx.ExecContext(ctx, `
UPDATE trajectory_upload_items
SET status = $3, action = NULLIF($4, 'failed'), error_code = NULLIF($5, ''), updated_at = NOW()
WHERE session_id = $1::uuid AND trajectory_id = $2 AND status = 'processing'`,
		sessionID, trajectoryKeyHash, map[bool]string{true: "failed", false: "completed"}[action == "failed"], action, errorCode)
	if err != nil {
		return err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if affected == 0 {
		return nil
	}
	column := "failed_records"
	switch action {
	case "inserted":
		column = "inserted_records"
	case "updated":
		column = "updated_records"
	case "skipped":
		column = "skipped_records"
	}
	_, err = tx.ExecContext(ctx, fmt.Sprintf(`
UPDATE trajectory_upload_sessions
SET %s = %s + 1, last_seen_at = NOW()
WHERE id = $1::uuid`, column, column), sessionID)
	if err != nil {
		return err
	}
	return tx.Commit()
}

func (s *postgresArchiveStore) GetUploadSession(ctx context.Context, tokenHash string, now time.Time) (uploadSessionState, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return uploadSessionState{}, err
	}
	defer func() { _ = tx.Rollback() }()
	state, err := lockUploadSession(ctx, tx, tokenHash, now)
	if err != nil {
		return uploadSessionState{}, err
	}
	if _, err := tx.ExecContext(ctx, `UPDATE trajectory_upload_sessions SET last_seen_at = $2 WHERE id = $1::uuid`, state.ID, now); err != nil {
		return uploadSessionState{}, err
	}
	return state, tx.Commit()
}

func (s *postgresArchiveStore) CompleteUploadSession(ctx context.Context, tokenHash string, now time.Time) (uploadSessionState, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return uploadSessionState{}, err
	}
	defer func() { _ = tx.Rollback() }()
	state, err := lockUploadSession(ctx, tx, tokenHash, now)
	if err != nil {
		return uploadSessionState{}, err
	}
	var processing int
	if err := tx.QueryRowContext(ctx, `
SELECT COUNT(*) FROM trajectory_upload_items
WHERE session_id = $1::uuid AND status = 'processing'`, state.ID).Scan(&processing); err != nil {
		return uploadSessionState{}, err
	}
	if processing > 0 {
		return uploadSessionState{}, errUploadHasProcessing
	}
	status := "completed"
	errorSummary := ""
	if state.Stats.Failed > 0 {
		status = "partial"
		errorSummary = "https_upload_record_failed"
	}
	_, err = tx.ExecContext(ctx, `
UPDATE trajectory_upload_sessions
SET status = 'completed', completed_at = $2, last_seen_at = $2
WHERE id = $1::uuid`, state.ID, now)
	if err != nil {
		return uploadSessionState{}, err
	}
	_, err = tx.ExecContext(ctx, `
UPDATE agent_trajectory_import_batches
SET status = $2, total_records = $3, inserted_records = $4,
    updated_records = $5, skipped_records = $6, failed_records = $7,
    error_summary = $8, completed_at = $9
WHERE id = $1::uuid`, state.ImportBatchID, status, state.Stats.Total,
		state.Stats.Inserted, state.Stats.Updated, state.Stats.Skipped,
		state.Stats.Failed, errorSummary, now)
	if err != nil {
		return uploadSessionState{}, err
	}
	if err := tx.Commit(); err != nil {
		return uploadSessionState{}, err
	}
	state.Status = "completed"
	state.CompletedAt = &now
	return state, nil
}

func lockUploadSession(ctx context.Context, tx *sql.Tx, tokenHash string, now time.Time) (uploadSessionState, error) {
	state := uploadSessionState{}
	var completedAt sql.NullTime
	err := tx.QueryRowContext(ctx, `
SELECT id::text, import_batch_id::text, user_id, status, max_bytes,
       received_bytes, total_records, inserted_records, updated_records,
       skipped_records, failed_records, created_at, expires_at, completed_at
FROM trajectory_upload_sessions
WHERE token_hash = $1
FOR UPDATE`, tokenHash).Scan(
		&state.ID, &state.ImportBatchID, &state.UserID, &state.Status, &state.MaxBytes,
		&state.ReceivedBytes, &state.Stats.Total, &state.Stats.Inserted,
		&state.Stats.Updated, &state.Stats.Skipped, &state.Stats.Failed,
		&state.CreatedAt, &state.ExpiresAt, &completedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return uploadSessionState{}, errUploadTokenInvalid
	}
	if err != nil {
		return uploadSessionState{}, err
	}
	if err := checkLabUploadGrant(ctx, tx, state.UserID, state.CreatedAt); err != nil {
		return uploadSessionState{}, err
	}
	if completedAt.Valid {
		value := completedAt.Time
		state.CompletedAt = &value
	}
	if !state.ExpiresAt.After(now) {
		_, _ = tx.ExecContext(ctx, `
UPDATE trajectory_upload_sessions
SET status = 'expired', completed_at = COALESCE(completed_at, $2), last_seen_at = $2
WHERE id = $1::uuid`, state.ID, now)
		if commitErr := tx.Commit(); commitErr != nil {
			return uploadSessionState{}, commitErr
		}
		return uploadSessionState{}, errUploadSessionExpired
	}
	if state.Status != "active" {
		return uploadSessionState{}, errUploadSessionClosed
	}
	return state, nil
}

func (s *postgresArchiveStore) CleanupUploadSessions(ctx context.Context, now time.Time) (int64, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer func() { _ = tx.Rollback() }()
	var changed int64
	result, err := tx.ExecContext(ctx, `
WITH stale AS (
  UPDATE trajectory_upload_items
  SET status = 'failed', error_code = 'processing_timeout', updated_at = $1
  WHERE status = 'processing' AND updated_at <= $1::timestamptz - INTERVAL '10 minutes'
  RETURNING session_id
), counts AS (
  SELECT session_id, COUNT(*)::INT AS failed_count
  FROM stale
  GROUP BY session_id
)
UPDATE trajectory_upload_sessions AS s
SET failed_records = s.failed_records + counts.failed_count,
    last_seen_at = $1
FROM counts
WHERE s.id = counts.session_id`, now)
	if err != nil {
		return 0, err
	}
	count, _ := result.RowsAffected()
	changed += count
	result, err = tx.ExecContext(ctx, `
UPDATE trajectory_upload_device_codes
SET status = 'expired'
WHERE status IN ('pending', 'approved') AND expires_at <= $1`, now)
	if err != nil {
		return 0, err
	}
	count, _ = result.RowsAffected()
	changed += count
	result, err = tx.ExecContext(ctx, `
UPDATE trajectory_upload_sessions
SET status = 'expired', completed_at = COALESCE(completed_at, $1)
WHERE status = 'active' AND expires_at <= $1`, now)
	if err != nil {
		return 0, err
	}
	count, _ = result.RowsAffected()
	changed += count
	_, err = tx.ExecContext(ctx, `
UPDATE agent_trajectory_import_batches AS b
SET status = CASE WHEN s.total_records = 0 THEN 'failed' ELSE 'partial' END,
    total_records = s.total_records,
    inserted_records = s.inserted_records,
    updated_records = s.updated_records,
    skipped_records = s.skipped_records,
    failed_records = s.failed_records,
    error_summary = 'https_upload_session_expired',
    completed_at = COALESCE(b.completed_at, $1)
FROM trajectory_upload_sessions AS s
WHERE s.import_batch_id = b.id AND s.status = 'expired' AND b.status = 'importing'`, now)
	if err != nil {
		return 0, err
	}
	result, err = tx.ExecContext(ctx, `
DELETE FROM trajectory_upload_device_codes
WHERE expires_at < $1::timestamptz - INTERVAL '24 hours'`, now)
	if err != nil {
		return 0, err
	}
	count, _ = result.RowsAffected()
	changed += count
	result, err = tx.ExecContext(ctx, `
DELETE FROM trajectory_upload_sessions
WHERE status IN ('completed', 'expired', 'revoked')
  AND COALESCE(completed_at, expires_at) < $1::timestamptz - INTERVAL '24 hours'`, now)
	if err != nil {
		return 0, err
	}
	count, _ = result.RowsAffected()
	changed += count
	return changed, tx.Commit()
}

func (s *postgresArchiveStore) CheckTrajectoryUploadCompatibility(ctx context.Context) error {
	var deviceTable, sessionTable, itemTable, migration bool
	err := s.db.QueryRowContext(ctx, `
SELECT
  to_regclass('public.trajectory_upload_device_codes') IS NOT NULL,
  to_regclass('public.trajectory_upload_sessions') IS NOT NULL,
  to_regclass('public.trajectory_upload_items') IS NOT NULL,
  EXISTS (SELECT 1 FROM conversation_archive_plugin_migrations WHERE name = '003_trajectory_https_upload')`).Scan(
		&deviceTable, &sessionTable, &itemTable, &migration)
	if err != nil {
		return err
	}
	if !deviceTable || !sessionTable || !itemTable || !migration {
		return fmt.Errorf("trajectory HTTPS upload migration is incomplete")
	}
	return nil
}

func (s *proxyServer) serveTrajectoryDevicePage(w http.ResponseWriter, req *http.Request) {
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
		_, _ = w.Write(trajectoryDeviceHTML)
	}
}

func (s *proxyServer) serveTrajectorySync(w http.ResponseWriter, req *http.Request) {
	req.Header.Del(archiveUserIDHeader)
	req.Header.Del(archiveAPIKeyIDHeader)
	req.Header.Del(clientRequestIDHeader)
	if s.uploadStore == nil {
		writeAPIError(w, http.StatusServiceUnavailable, 50320, "Trajectory upload is unavailable")
		return
	}
	switch req.URL.Path {
	case "/_archive/api/v1/trajectory-sync/device/start":
		s.startTrajectoryDevice(w, req)
	case "/_archive/api/v1/trajectory-sync/device/approve":
		s.approveTrajectoryDevice(w, req)
	case "/_archive/api/v1/trajectory-sync/device/token":
		s.exchangeTrajectoryDevice(w, req)
	case "/_archive/api/v1/trajectory-sync/trajectories":
		s.uploadTrajectory(w, req)
	case "/_archive/api/v1/trajectory-sync/session":
		s.getTrajectoryUploadSession(w, req)
	case "/_archive/api/v1/trajectory-sync/complete":
		s.completeTrajectoryUploadSession(w, req)
	default:
		writeAPIError(w, http.StatusNotFound, 40420, "Trajectory upload endpoint not found")
	}
}

func (s *proxyServer) startTrajectoryDevice(w http.ResponseWriter, req *http.Request) {
	if req.Method != http.MethodPost {
		writeAPIError(w, http.StatusMethodNotAllowed, 40520, "Method not allowed")
		return
	}
	var input struct{}
	if err := decodeSmallUploadJSON(w, req, &input); err != nil {
		writeAPIError(w, http.StatusBadRequest, 40019, "Invalid device authorization request")
		return
	}
	origin, err := s.archivePublicOrigin(req)
	if err != nil {
		writeAPIError(w, http.StatusServiceUnavailable, 50321, "Public HTTPS archive URL is not configured")
		return
	}
	deviceCode, err := randomOpaqueSecret(32)
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, 50020, "Could not create device authorization")
		return
	}
	userCode, err := randomUserCode()
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, 50020, "Could not create device authorization")
		return
	}
	id, err := randomUUIDString()
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, 50020, "Could not create device authorization")
		return
	}
	now := time.Now().UTC()
	pollSeconds := int(s.cfg.UploadPollInterval / time.Second)
	if pollSeconds < 1 {
		pollSeconds = 3
	}
	ctx, cancel := context.WithTimeout(req.Context(), s.cfg.DatabaseTimeout)
	err = s.uploadStore.CreateUploadDeviceCode(ctx, uploadDeviceCreate{
		ID: id, DeviceCodeHash: hashSecret(deviceCode), UserCodeHash: hashSecret(normalizeUserCode(userCode)),
		PollIntervalSeconds: pollSeconds, ExpiresAt: now.Add(s.cfg.UploadDeviceTTL),
	})
	cancel()
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, 50021, "Could not persist device authorization")
		return
	}
	verificationURI := strings.TrimRight(origin, "/") + "/_archive/device/"
	writeAPISuccess(w, map[string]any{
		"device_code":               deviceCode,
		"user_code":                 userCode,
		"verification_uri":          verificationURI,
		"verification_uri_complete": verificationURI + "?user_code=" + url.QueryEscape(userCode),
		"expires_in":                int(s.cfg.UploadDeviceTTL / time.Second),
		"interval":                  pollSeconds,
	})
}

func (s *proxyServer) approveTrajectoryDevice(w http.ResponseWriter, req *http.Request) {
	if req.Method != http.MethodPost {
		writeAPIError(w, http.StatusMethodNotAllowed, 40521, "Method not allowed")
		return
	}
	identity, status, err := s.validateSub2APIUser(req)
	if err != nil {
		s.metrics.adminAuthFailures.Add(1)
		writeAPIError(w, status, status*100, err.Error())
		return
	}
	var input struct {
		UserCode string `json:"user_code"`
	}
	if err := decodeSmallUploadJSON(w, req, &input); err != nil {
		writeAPIError(w, http.StatusBadRequest, 40020, "Invalid device approval request")
		return
	}
	code := normalizeUserCode(input.UserCode)
	if len(code) != 8 {
		writeAPIError(w, http.StatusBadRequest, 40021, "Invalid user code")
		return
	}
	ctx, cancel := context.WithTimeout(req.Context(), s.cfg.DatabaseTimeout)
	err = s.uploadStore.ApproveUploadDeviceCode(ctx, hashSecret(code), identity, time.Now().UTC())
	cancel()
	if err != nil {
		s.metrics.uploadRejected.Add(1)
		switch {
		case errors.Is(err, errUploadDeviceNotFound):
			writeAPIError(w, http.StatusNotFound, 40421, "Device code was not found")
		case errors.Is(err, errUploadDeviceExpired):
			writeAPIError(w, http.StatusGone, 41021, "Device code has expired")
		case errors.Is(err, errUploadDeviceConsumed):
			writeAPIError(w, http.StatusConflict, 40921, "Device code has already been used")
		default:
			writeAPIError(w, http.StatusConflict, 40922, "Device authorization could not be approved")
		}
		return
	}
	s.metrics.uploadAuthorized.Add(1)
	writeAPISuccess(w, map[string]any{"user_id": identity.ID, "email": identity.Email, "role": identity.Role})
}

func (s *proxyServer) exchangeTrajectoryDevice(w http.ResponseWriter, req *http.Request) {
	if req.Method != http.MethodPost {
		writeAPIError(w, http.StatusMethodNotAllowed, 40522, "Method not allowed")
		return
	}
	var input struct {
		DeviceCode string `json:"device_code"`
	}
	if err := decodeSmallUploadJSON(w, req, &input); err != nil || len(input.DeviceCode) < 32 {
		writeAPIError(w, http.StatusBadRequest, 40022, "Invalid device token request")
		return
	}
	sessionID, err := randomUUIDString()
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, 50022, "Could not create upload session")
		return
	}
	batchID, err := randomUUIDString()
	if err != nil {
		writeAPIError(w, http.StatusInternalServerError, 50022, "Could not create upload session")
		return
	}
	uploadToken := deriveUploadToken(input.DeviceCode)
	now := time.Now().UTC()
	exchange := uploadDeviceExchange{
		DeviceCodeHash: hashSecret(input.DeviceCode), SessionID: sessionID, BatchID: batchID,
		TokenHash: hashSecret(uploadToken), ManifestHash: hashSecret("https-session:" + sessionID),
		Now: now, ExpiresAt: now.Add(s.cfg.UploadSessionTTL), MaxBytes: s.cfg.UploadMaxBytes,
	}
	ctx, cancel := context.WithTimeout(req.Context(), s.cfg.DatabaseTimeout)
	state, err := s.uploadStore.ExchangeUploadDeviceCode(ctx, exchange)
	cancel()
	if err != nil {
		switch {
		case errors.Is(err, errUploadPending):
			writeAPIError(w, http.StatusPreconditionRequired, 42820, "authorization_pending")
		case errors.Is(err, errUploadSlowDown):
			w.Header().Set("Retry-After", "5")
			writeAPIError(w, http.StatusTooManyRequests, 42920, "slow_down")
		case errors.Is(err, errUploadDeviceExpired):
			writeAPIError(w, http.StatusGone, 41022, "expired_token")
		case errors.Is(err, errUploadDeviceConsumed):
			writeAPIError(w, http.StatusGone, 41023, "device_code_consumed")
		case errors.Is(err, errUploadDeviceNotFound):
			writeAPIError(w, http.StatusUnauthorized, 40120, "invalid_device_code")
		default:
			writeAPIError(w, http.StatusInternalServerError, 50023, "Could not exchange device authorization")
		}
		return
	}
	s.metrics.uploadSessions.Add(1)
	writeAPISuccess(w, map[string]any{
		"upload_token": uploadToken,
		"token_type":   "opaque",
		"expires_in":   int(time.Until(state.ExpiresAt).Seconds()),
		"session":      state,
	})
}

func (s *proxyServer) uploadTrajectory(w http.ResponseWriter, req *http.Request) {
	if req.Method != http.MethodPost {
		writeAPIError(w, http.StatusMethodNotAllowed, 40523, "Method not allowed")
		return
	}
	token, ok := trajectoryUploadToken(req)
	if !ok {
		s.metrics.uploadRejected.Add(1)
		writeAPIError(w, http.StatusUnauthorized, 40121, "Upload token is required")
		return
	}
	raw, err := readUploadRecord(w, req, s.cfg.UploadMaxRecordBytes)
	if err != nil {
		s.metrics.uploadRejected.Add(1)
		if errors.Is(err, errBodyTooLarge) {
			writeAPIError(w, http.StatusRequestEntityTooLarge, 41320, "Trajectory record exceeds the upload limit")
		} else {
			writeAPIError(w, http.StatusBadRequest, 40023, "Trajectory record could not be read")
		}
		return
	}
	var record trajectoryImportRecord
	if err := decodeStrictJSON(raw, &record); err != nil {
		s.metrics.uploadRejected.Add(1)
		writeAPIError(w, http.StatusBadRequest, 40024, "Trajectory record is invalid: malformed JSON")
		return
	}
	if err := canonicalizeTrajectoryTerminal(&record); err != nil {
		s.metrics.uploadRejected.Add(1)
		writeAPIError(w, http.StatusBadRequest, 40024, "Trajectory record is invalid: "+err.Error())
		return
	}
	if err := validateTrajectoryRecord(record, time.Now().UTC()); err != nil {
		s.metrics.uploadRejected.Add(1)
		if errors.Is(err, errTrajectoryExpired) {
			writeAPIError(w, http.StatusGone, 41024, "Trajectory has already expired")
		} else {
			writeAPIError(w, http.StatusBadRequest, 40024, "Trajectory record is invalid: "+err.Error())
		}
		return
	}
	if req.Context().Err() != nil {
		return
	}
	keyHash := hashSecret(record.TrajectoryID)
	requestHash := keyedUploadHash(token, raw)
	now := time.Now().UTC()
	ctx, cancel := context.WithTimeout(req.Context(), s.cfg.DatabaseTimeout)
	reservation, err := s.uploadStore.BeginUploadItem(ctx, uploadItemBegin{
		TokenHash: hashSecret(token), TrajectoryKeyHash: keyHash,
		RequestHash: requestHash, RequestBytes: int64(len(raw)), Now: now,
	})
	cancel()
	if err != nil {
		s.metrics.uploadRejected.Add(1)
		writeUploadSessionError(w, err)
		return
	}
	if reservation.Replay {
		s.metrics.uploadReplays.Add(1)
		writeAPISuccess(w, map[string]any{"trajectory_id": record.TrajectoryID, "action": reservation.Action, "replayed": true})
		return
	}
	finished := false
	defer func() {
		if finished {
			return
		}
		cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), s.cfg.DatabaseTimeout)
		_ = s.uploadStore.FinishUploadItem(cleanupCtx, reservation.SessionID, keyHash, "failed", "request_interrupted")
		cleanupCancel()
	}()
	privacyCtx, privacyCancel := context.WithTimeout(req.Context(), s.cfg.PrivacyTimeout)
	err = redactTrajectoryRecordTwice(privacyCtx, s.privacy, &record)
	privacyCancel()
	if err != nil {
		s.metrics.privacyFailures.Add(1)
		s.metrics.uploadFailures.Add(1)
		finishCtx, finishCancel := context.WithTimeout(context.Background(), s.cfg.DatabaseTimeout)
		_ = s.uploadStore.FinishUploadItem(finishCtx, reservation.SessionID, keyHash, "failed", "privacy_filter_failed")
		finishCancel()
		finished = true
		writeAPIError(w, http.StatusServiceUnavailable, 50322, "Privacy filtering failed")
		return
	}
	if err := validateTrajectoryRecord(record, time.Now().UTC()); err != nil {
		s.metrics.uploadFailures.Add(1)
		finishCtx, finishCancel := context.WithTimeout(context.Background(), s.cfg.DatabaseTimeout)
		_ = s.uploadStore.FinishUploadItem(finishCtx, reservation.SessionID, keyHash, "failed", "post_filter_validation_failed")
		finishCancel()
		finished = true
		writeAPIError(w, http.StatusInternalServerError, 50024, "Filtered trajectory validation failed")
		return
	}
	if req.Context().Err() != nil {
		return
	}
	writeCtx, writeCancel := context.WithTimeout(req.Context(), s.cfg.DatabaseTimeout)
	action, err := s.uploadStore.ImportTrajectory(writeCtx, reservation.ImportBatchID, reservation.UserID, record)
	writeCancel()
	if err != nil {
		s.metrics.uploadFailures.Add(1)
		finishCtx, finishCancel := context.WithTimeout(context.Background(), s.cfg.DatabaseTimeout)
		_ = s.uploadStore.FinishUploadItem(finishCtx, reservation.SessionID, keyHash, "failed", "database_write_failed")
		finishCancel()
		finished = true
		writeAPIError(w, http.StatusInternalServerError, 50025, "Trajectory could not be stored")
		return
	}
	if action != "inserted" && action != "updated" {
		action = "skipped"
	}
	finishCtx, finishCancel := context.WithTimeout(context.Background(), s.cfg.DatabaseTimeout)
	err = s.uploadStore.FinishUploadItem(finishCtx, reservation.SessionID, keyHash, action, "")
	finishCancel()
	if err != nil {
		s.metrics.uploadFailures.Add(1)
		writeAPIError(w, http.StatusInternalServerError, 50026, "Trajectory upload state could not be finalized")
		return
	}
	finished = true
	s.metrics.uploadRecords.Add(1)
	writeAPISuccess(w, map[string]any{"trajectory_id": record.TrajectoryID, "action": action, "replayed": false})
}

func (s *proxyServer) getTrajectoryUploadSession(w http.ResponseWriter, req *http.Request) {
	if req.Method != http.MethodGet {
		writeAPIError(w, http.StatusMethodNotAllowed, 40524, "Method not allowed")
		return
	}
	token, ok := trajectoryUploadToken(req)
	if !ok {
		writeAPIError(w, http.StatusUnauthorized, 40121, "Upload token is required")
		return
	}
	ctx, cancel := context.WithTimeout(req.Context(), s.cfg.DatabaseTimeout)
	state, err := s.uploadStore.GetUploadSession(ctx, hashSecret(token), time.Now().UTC())
	cancel()
	if err != nil {
		writeUploadSessionError(w, err)
		return
	}
	writeAPISuccess(w, state)
}

func (s *proxyServer) completeTrajectoryUploadSession(w http.ResponseWriter, req *http.Request) {
	if req.Method != http.MethodPost {
		writeAPIError(w, http.StatusMethodNotAllowed, 40525, "Method not allowed")
		return
	}
	var input struct{}
	if err := decodeSmallUploadJSON(w, req, &input); err != nil {
		writeAPIError(w, http.StatusBadRequest, 40025, "Invalid upload completion request")
		return
	}
	token, ok := trajectoryUploadToken(req)
	if !ok {
		writeAPIError(w, http.StatusUnauthorized, 40121, "Upload token is required")
		return
	}
	ctx, cancel := context.WithTimeout(req.Context(), s.cfg.DatabaseTimeout)
	state, err := s.uploadStore.CompleteUploadSession(ctx, hashSecret(token), time.Now().UTC())
	cancel()
	if err != nil {
		writeUploadSessionError(w, err)
		return
	}
	writeAPISuccess(w, state)
}

func writeUploadSessionError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, errUploadTokenInvalid):
		writeAPIError(w, http.StatusUnauthorized, 40122, "Upload token is invalid")
	case errors.Is(err, errUploadSessionExpired):
		writeAPIError(w, http.StatusGone, 41025, "Upload session has expired")
	case errors.Is(err, errUploadSessionClosed):
		writeAPIError(w, http.StatusGone, 41026, "Upload session is closed")
	case errors.Is(err, errUploadLimitExceeded):
		writeAPIError(w, http.StatusRequestEntityTooLarge, 41321, "Upload session byte limit exceeded")
	case errors.Is(err, errUploadReplayConflict):
		writeAPIError(w, http.StatusConflict, 40923, "Trajectory id was replayed with different content")
	case errors.Is(err, errUploadInProgress), errors.Is(err, errUploadHasProcessing):
		writeAPIError(w, http.StatusConflict, 40924, "Trajectory upload is still processing")
	default:
		writeAPIError(w, http.StatusInternalServerError, 50027, "Trajectory upload request failed")
	}
}

func decodeSmallUploadJSON(w http.ResponseWriter, req *http.Request, target any) error {
	raw, err := readUploadRecord(w, req, 16<<10)
	if err != nil {
		return err
	}
	return decodeStrictJSON(raw, target)
}

func readUploadRecord(w http.ResponseWriter, req *http.Request, limit int64) ([]byte, error) {
	if limit <= 0 {
		limit = maxTrajectoryRecordBytes
	}
	if req.ContentLength > limit {
		return nil, errBodyTooLarge
	}
	body := http.MaxBytesReader(w, req.Body, limit)
	defer func() { _ = body.Close() }()
	raw, err := io.ReadAll(body)
	if err != nil {
		var maxErr *http.MaxBytesError
		if errors.As(err, &maxErr) {
			return nil, errBodyTooLarge
		}
		return nil, err
	}
	if len(bytes.TrimSpace(raw)) == 0 {
		return nil, fmt.Errorf("empty request")
	}
	return raw, nil
}

func decodeStrictJSON(raw []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	if decoder.Decode(&struct{}{}) != io.EOF {
		return fmt.Errorf("multiple JSON values")
	}
	return nil
}

func trajectoryUploadToken(req *http.Request) (string, bool) {
	token := strings.TrimSpace(req.Header.Get(uploadTokenHeader))
	return token, len(token) >= 32 && len(token) <= 512
}

func hashSecret(value string) string {
	digest := sha256.Sum256([]byte(value))
	return hex.EncodeToString(digest[:])
}

func keyedUploadHash(token string, raw []byte) string {
	digest := hmac.New(sha256.New, []byte(token))
	_, _ = digest.Write(raw)
	return hex.EncodeToString(digest.Sum(nil))
}

func deriveUploadToken(deviceCode string) string {
	digest := sha256.Sum256([]byte("trajectory-upload-token\x00" + deviceCode))
	return base64.RawURLEncoding.EncodeToString(digest[:])
}

func randomOpaqueSecret(size int) (string, error) {
	value := make([]byte, size)
	if _, err := rand.Read(value); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(value), nil
}

func randomUUIDString() (string, error) {
	value := make([]byte, 16)
	if _, err := rand.Read(value); err != nil {
		return "", err
	}
	value[6] = (value[6] & 0x0f) | 0x40
	value[8] = (value[8] & 0x3f) | 0x80
	hexValue := hex.EncodeToString(value)
	return hexValue[0:8] + "-" + hexValue[8:12] + "-" + hexValue[12:16] + "-" + hexValue[16:20] + "-" + hexValue[20:32], nil
}

func randomUserCode() (string, error) {
	const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
	random := make([]byte, 8)
	if _, err := rand.Read(random); err != nil {
		return "", err
	}
	for index := range random {
		random[index] = alphabet[int(random[index])%len(alphabet)]
	}
	return string(random[:4]) + "-" + string(random[4:]), nil
}

func normalizeUserCode(value string) string {
	value = strings.ToUpper(strings.TrimSpace(value))
	value = strings.ReplaceAll(value, "-", "")
	value = strings.ReplaceAll(value, " ", "")
	return value
}

func (s *proxyServer) archivePublicOrigin(req *http.Request) (string, error) {
	if s.cfg.ArchivePublicURL != nil {
		return strings.TrimRight(s.cfg.ArchivePublicURL.String(), "/"), nil
	}
	scheme := strings.TrimSpace(strings.Split(req.Header.Get("X-Forwarded-Proto"), ",")[0])
	if scheme == "" {
		if req.TLS != nil {
			scheme = "https"
		} else {
			scheme = "http"
		}
	}
	host := strings.TrimSpace(strings.Split(req.Header.Get("X-Forwarded-Host"), ",")[0])
	if host == "" {
		host = req.Host
	}
	if host == "" || (scheme != "https" && !loopbackHost(host)) {
		return "", fmt.Errorf("public origin is not HTTPS")
	}
	return (&url.URL{Scheme: scheme, Host: host}).String(), nil
}

func loopbackHost(hostport string) bool {
	host := hostport
	if parsed, _, err := net.SplitHostPort(hostport); err == nil {
		host = parsed
	}
	host = strings.Trim(host, "[]")
	return host == "localhost" || net.ParseIP(host).IsLoopback()
}

func (s *proxyServer) startUploadCleanupWorker(ctx context.Context) {
	if s.uploadStore == nil {
		return
	}
	run := func() {
		opCtx, cancel := context.WithTimeout(ctx, s.cfg.DatabaseTimeout)
		changed, err := s.uploadStore.CleanupUploadSessions(opCtx, time.Now().UTC())
		cancel()
		if err != nil {
			log.Printf(`{"event":"trajectory_upload_cleanup_failed","error":%q}`, err.Error())
			return
		}
		if changed > 0 {
			log.Printf(`{"event":"trajectory_upload_cleanup","changed":%d}`, changed)
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
