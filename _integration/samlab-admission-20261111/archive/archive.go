package main

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/lib/pq"
)

type archiveInsert struct {
	UserID          int64
	APIKeyID        int64
	ClientRequestID string
	RequestID       string
	Protocol        string
	Endpoint        string
	Model           string
	RequestPayload  json.RawMessage
	AssistantText   string
	ToolCalls       json.RawMessage
	FinalResponse   json.RawMessage
	StartedAt       time.Time
	CompletedAt     time.Time
	DurationMS      int64
	ResponseStatus  int
	PrivacyEntities int
	ExpiresAt       time.Time
}

type completedArchive struct {
	ClientRequestID string
	RequestID       string
	Protocol        string
	Endpoint        string
	Model           string
	RequestPayload  json.RawMessage
	AssistantText   string
	ToolCalls       json.RawMessage
	FinalResponse   json.RawMessage
	StartedAt       time.Time
	CompletedAt     time.Time
	DurationMS      int64
	ResponseStatus  int
	PrivacyEntities int
	ExpiresAt       time.Time
	AttemptCount    int
	NextAttemptAt   time.Time
	DeadlineAt      time.Time
}

type archiveQueueResult struct {
	Queued   bool
	Inserted bool
}

type archiveResolveStats struct {
	Resolved  int64
	Inserted  int64
	Retried   int64
	TimedOut  int64
	Ambiguous int64
}

type archiveQuery struct {
	Page      int
	PageSize  int
	UserID    *int64
	APIKeyID  *int64
	Model     string
	Protocol  string
	StartTime *time.Time
	EndTime   *time.Time
}

type conversationArchive struct {
	ID              int64           `json:"id"`
	UserID          int64           `json:"user_id"`
	APIKeyID        *int64          `json:"api_key_id"`
	ClientRequestID *string         `json:"client_request_id,omitempty"`
	RequestID       string          `json:"request_id"`
	Protocol        string          `json:"protocol"`
	Endpoint        string          `json:"endpoint"`
	Model           string          `json:"model"`
	RequestPayload  json.RawMessage `json:"request_payload,omitempty"`
	AssistantText   string          `json:"assistant_text,omitempty"`
	ToolCalls       json.RawMessage `json:"tool_calls,omitempty"`
	FinalResponse   json.RawMessage `json:"final_response,omitempty"`
	StartedAt       time.Time       `json:"started_at"`
	CompletedAt     time.Time       `json:"completed_at"`
	DurationMS      int64           `json:"duration_ms"`
	ResponseStatus  int             `json:"response_status"`
	PrivacyEntities int             `json:"privacy_entities"`
	ExpiresAt       time.Time       `json:"expires_at"`
}

type archivePage struct {
	Items    []*conversationArchive `json:"items"`
	Total    int64                  `json:"total"`
	Page     int                    `json:"page"`
	PageSize int                    `json:"page_size"`
	Pages    int                    `json:"pages"`
}

type archiveStore interface {
	QueueCompleted(context.Context, completedArchive) (archiveQueueResult, error)
	ResolveDue(context.Context, time.Time, int) (archiveResolveStats, error)
	DeleteExpired(context.Context, time.Time, int) (int64, error)
	ListArchives(context.Context, archiveQuery) (archivePage, error)
	GetArchive(context.Context, int64) (*conversationArchive, error)
	ResolveArchiveUserByEmail(context.Context, string) (archiveUser, error)
	StartTrajectoryBatch(context.Context, trajectoryBatchStart) (trajectoryBatchState, error)
	ImportTrajectory(context.Context, string, int64, trajectoryImportRecord) (string, error)
	FinishTrajectoryBatch(context.Context, string, trajectoryImportStats, string) error
	ListTrajectories(context.Context, trajectoryQuery) (trajectoryPage, error)
	GetTrajectory(context.Context, int64) (*agentTrajectory, error)
	ListTrajectoryEvents(context.Context, int64, int, int) (trajectoryEventPage, error)
	CheckCompatibility(context.Context) error
	Ping(context.Context) error
	Close() error
}

type postgresArchiveStore struct {
	db *sql.DB
}

func newPostgresArchiveStore(databaseURL string) (*postgresArchiveStore, error) {
	db, err := sql.Open("postgres", databaseURL)
	if err != nil {
		return nil, fmt.Errorf("open archive database: %w", err)
	}
	db.SetMaxOpenConns(8)
	db.SetMaxIdleConns(3)
	db.SetConnMaxLifetime(30 * time.Minute)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("connect archive database: %w", err)
	}
	return &postgresArchiveStore{db: db}, nil
}

func (s *postgresArchiveStore) QueueCompleted(ctx context.Context, item completedArchive) (archiveQueueResult, error) {
	// Persist the completed conversation before attempting identity resolution.
	// usage_logs is written asynchronously by Sub2API, so absence of an identity
	// at this point is normal and must never roll back the completed payload.
	result, err := s.db.ExecContext(ctx, `
INSERT INTO conversation_archive_pending (
  client_request_id, request_id, protocol, endpoint, model,
  request_payload, assistant_text, tool_calls, final_response,
  started_at, completed_at, duration_ms, response_status,
  privacy_entities, expires_at, next_attempt_at, deadline_at
) VALUES (
  $1::uuid, $2, $3, $4, $5,
  $6, $7, $8, $9,
  $10, $11, $12, $13, $14, $15, NOW(), $16
)
ON CONFLICT (client_request_id) DO NOTHING`,
		item.ClientRequestID, item.RequestID, item.Protocol, item.Endpoint, item.Model,
		item.RequestPayload, item.AssistantText, item.ToolCalls, item.FinalResponse,
		item.StartedAt, item.CompletedAt, item.DurationMS, item.ResponseStatus,
		item.PrivacyEntities, item.ExpiresAt, item.DeadlineAt)
	if err != nil {
		return archiveQueueResult{}, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return archiveQueueResult{}, err
	}
	return archiveQueueResult{Queued: affected == 1}, nil
}

func finalizeCompletedTx(ctx context.Context, tx *sql.Tx, item completedArchive) (bool, bool, error) {
	userID, apiKeyID, matched, err := resolveArchiveIdentity(ctx, tx, item)
	if err != nil || !matched {
		return false, matched, err
	}
	result, err := tx.ExecContext(ctx, `
INSERT INTO conversation_archives (
  user_id, api_key_id, client_request_id, request_id, protocol, endpoint, model,
  request_payload, assistant_text, tool_calls, final_response,
  started_at, completed_at, duration_ms, response_status,
  privacy_entities, expires_at
) VALUES (
  $1, $2, $3::uuid, $4, $5, $6, $7,
  $8, $9, $10, $11,
  $12, $13, $14, $15, $16, $17
)
ON CONFLICT (request_id) DO NOTHING`,
		userID, apiKeyID, item.ClientRequestID, item.RequestID, item.Protocol, item.Endpoint, item.Model,
		item.RequestPayload, item.AssistantText, item.ToolCalls, item.FinalResponse,
		item.StartedAt, item.CompletedAt, item.DurationMS, item.ResponseStatus,
		item.PrivacyEntities, item.ExpiresAt)
	if err != nil {
		return false, true, err
	}
	if _, err := tx.ExecContext(ctx, `DELETE FROM conversation_archive_pending WHERE client_request_id = $1::uuid`, item.ClientRequestID); err != nil {
		return false, true, err
	}
	affected, err := result.RowsAffected()
	return affected == 1, true, err
}

type queryRower interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}

func resolveArchiveIdentity(ctx context.Context, queryer queryRower, item completedArchive) (int64, int64, bool, error) {
	var userID, apiKeyID int64
	err := queryer.QueryRowContext(ctx, `
SELECT user_id, api_key_id
FROM resolve_archive_identity($1::uuid, $2, $3)`,
		item.ClientRequestID, item.StartedAt, item.CompletedAt).Scan(&userID, &apiKeyID)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, 0, false, nil
	}
	if err != nil {
		return 0, 0, false, err
	}
	if userID <= 0 || apiKeyID <= 0 {
		return 0, 0, false, fmt.Errorf("resolver returned invalid identity")
	}
	return userID, apiKeyID, true, nil
}

func (s *postgresArchiveStore) ResolveDue(ctx context.Context, now time.Time, batchSize int) (archiveResolveStats, error) {
	stats := archiveResolveStats{}
	for index := 0; index < batchSize; index++ {
		tx, err := s.db.BeginTx(ctx, nil)
		if err != nil {
			return stats, err
		}
		item, found, err := loadDuePending(ctx, tx, now)
		if err != nil {
			_ = tx.Rollback()
			return stats, err
		}
		if !found {
			_ = tx.Rollback()
			break
		}

		inserted, matched, resolveErr := finalizeCompletedTx(ctx, tx, item)
		if resolveErr == nil && matched {
			if err := tx.Commit(); err != nil {
				return stats, err
			}
			stats.Resolved++
			if inserted {
				stats.Inserted++
			}
			continue
		}
		if resolveErr != nil {
			// A failed PostgreSQL statement aborts the current transaction. Roll it
			// back before updating or deleting the durable pending record.
			_ = tx.Rollback()
			if isAmbiguousArchiveIdentityError(resolveErr) {
				if err := s.deletePending(ctx, item.ClientRequestID); err != nil {
					return stats, fmt.Errorf("delete ambiguous pending archive: %w", err)
				}
				stats.Ambiguous++
				continue
			}
			if !now.Before(item.DeadlineAt) {
				if err := s.deletePending(ctx, item.ClientRequestID); err != nil {
					return stats, fmt.Errorf("delete expired unresolved archive: %w", err)
				}
				stats.TimedOut++
				continue
			}
			if err := s.schedulePendingRetry(ctx, item, now, resolveErr); err != nil {
				return stats, fmt.Errorf("record identity resolver failure: %w (resolver error: %v)", err, resolveErr)
			}
			stats.Retried++
			return stats, fmt.Errorf("resolve archive identity for %s: %w", item.ClientRequestID, resolveErr)
		}

		if !matched && !now.Before(item.DeadlineAt) {
			if _, err := tx.ExecContext(ctx, `DELETE FROM conversation_archive_pending WHERE client_request_id = $1::uuid`, item.ClientRequestID); err != nil {
				_ = tx.Rollback()
				return stats, err
			}
			if err := tx.Commit(); err != nil {
				return stats, err
			}
			stats.TimedOut++
			continue
		}

		if _, err := tx.ExecContext(ctx, `
UPDATE conversation_archive_pending
SET attempt_count = attempt_count + 1,
    next_attempt_at = $2,
    last_error = $3
WHERE client_request_id = $1::uuid`, item.ClientRequestID,
			now.Add(identityBackoff(item.AttemptCount+1)), "identity_not_available"); err != nil {
			_ = tx.Rollback()
			return stats, err
		}
		if err := tx.Commit(); err != nil {
			return stats, err
		}
		stats.Retried++
	}
	return stats, nil
}

func isAmbiguousArchiveIdentityError(err error) bool {
	var postgresError *pq.Error
	if !errors.As(err, &postgresError) {
		return false
	}
	return postgresError.Code == "P0001" &&
		(postgresError.Detail == "archive_identity_ambiguous" ||
			strings.HasPrefix(postgresError.Message, "ambiguous archive identity"))
}

func (s *postgresArchiveStore) schedulePendingRetry(
	ctx context.Context,
	item completedArchive,
	now time.Time,
	resolveErr error,
) error {
	result, err := s.db.ExecContext(ctx, `
UPDATE conversation_archive_pending
SET attempt_count = attempt_count + 1,
    next_attempt_at = $2,
    last_error = $3
WHERE client_request_id = $1::uuid`, item.ClientRequestID,
		now.Add(identityBackoff(item.AttemptCount+1)), truncateError(resolveErr.Error(), 500))
	if err != nil {
		return err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if affected != 1 {
		return fmt.Errorf("pending archive no longer exists")
	}
	return nil
}

func (s *postgresArchiveStore) deletePending(ctx context.Context, clientRequestID string) error {
	result, err := s.db.ExecContext(ctx,
		`DELETE FROM conversation_archive_pending WHERE client_request_id = $1::uuid`, clientRequestID)
	if err != nil {
		return err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if affected != 1 {
		return fmt.Errorf("pending archive no longer exists")
	}
	return nil
}

func loadDuePending(ctx context.Context, tx *sql.Tx, now time.Time) (completedArchive, bool, error) {
	item := completedArchive{}
	err := tx.QueryRowContext(ctx, `
SELECT client_request_id::text, request_id, protocol, endpoint, model,
       request_payload, assistant_text, tool_calls, final_response,
       started_at, completed_at, duration_ms, response_status,
       privacy_entities, expires_at, attempt_count, next_attempt_at, deadline_at
FROM conversation_archive_pending
WHERE next_attempt_at <= $1
ORDER BY next_attempt_at, id
FOR UPDATE SKIP LOCKED
LIMIT 1`, now.UTC()).Scan(
		&item.ClientRequestID, &item.RequestID, &item.Protocol, &item.Endpoint, &item.Model,
		&item.RequestPayload, &item.AssistantText, &item.ToolCalls, &item.FinalResponse,
		&item.StartedAt, &item.CompletedAt, &item.DurationMS, &item.ResponseStatus,
		&item.PrivacyEntities, &item.ExpiresAt, &item.AttemptCount, &item.NextAttemptAt, &item.DeadlineAt)
	if errors.Is(err, sql.ErrNoRows) {
		return completedArchive{}, false, nil
	}
	return item, err == nil, err
}

func identityBackoff(attempt int) time.Duration {
	switch {
	case attempt <= 1:
		return 250 * time.Millisecond
	case attempt == 2:
		return 500 * time.Millisecond
	case attempt == 3:
		return time.Second
	case attempt == 4:
		return 2 * time.Second
	case attempt == 5:
		return 4 * time.Second
	default:
		return 5 * time.Second
	}
}

func truncateError(value string, limit int) string {
	if len(value) <= limit {
		return value
	}
	return value[:limit]
}

func (s *postgresArchiveStore) DeleteExpired(ctx context.Context, now time.Time, batchSize int) (int64, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	defer func() { _ = tx.Rollback() }()
	result, err := tx.ExecContext(ctx, `
WITH expired AS (
  SELECT id
  FROM conversation_archives
  WHERE expires_at < $1
  ORDER BY expires_at, id
  LIMIT $2
)
DELETE FROM conversation_archives
WHERE id IN (SELECT id FROM expired)`, now.UTC(), batchSize)
	if err != nil {
		return 0, err
	}
	deletedArchives, err := result.RowsAffected()
	if err != nil {
		return 0, err
	}
	remaining := int64(batchSize) - deletedArchives
	var deletedPending int64
	if remaining > 0 {
		result, err = tx.ExecContext(ctx, `
WITH expired AS (
  SELECT id
  FROM conversation_archive_pending
  WHERE expires_at < $1
  ORDER BY expires_at, id
  LIMIT $2
)
DELETE FROM conversation_archive_pending
WHERE id IN (SELECT id FROM expired)`, now.UTC(), remaining)
		if err != nil {
			return 0, err
		}
		deletedPending, err = result.RowsAffected()
		if err != nil {
			return 0, err
		}
	}
	remaining -= deletedPending
	var deletedTrajectories int64
	if remaining > 0 {
		result, err = tx.ExecContext(ctx, `
WITH expired AS (
  SELECT id
  FROM agent_trajectories
  WHERE expires_at < $1
  ORDER BY expires_at, id
  LIMIT $2
)
DELETE FROM agent_trajectories
WHERE id IN (SELECT id FROM expired)`, now.UTC(), remaining)
		if err != nil {
			return 0, err
		}
		deletedTrajectories, err = result.RowsAffected()
		if err != nil {
			return 0, err
		}
	}
	remaining -= deletedTrajectories
	var deletedBatches int64
	if remaining > 0 {
		result, err = tx.ExecContext(ctx, `
WITH expired AS (
  SELECT b.id
  FROM agent_trajectory_import_batches b
  WHERE b.created_at < $1::TIMESTAMPTZ - INTERVAL '30 days'
    AND NOT EXISTS (
      SELECT 1 FROM agent_trajectories t WHERE t.import_batch_id = b.id
    )
  ORDER BY b.created_at, b.id
  LIMIT $2
)
DELETE FROM agent_trajectory_import_batches
WHERE id IN (SELECT id FROM expired)`, now.UTC(), remaining)
		if err != nil {
			return 0, err
		}
		deletedBatches, err = result.RowsAffected()
		if err != nil {
			return 0, err
		}
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return deletedArchives + deletedPending + deletedTrajectories + deletedBatches, nil
}

func (s *postgresArchiveStore) ListArchives(ctx context.Context, query archiveQuery) (archivePage, error) {
	where := []string{"1=1"}
	args := make([]any, 0, 10)
	add := func(clause string, value any) {
		args = append(args, value)
		where = append(where, fmt.Sprintf(clause, len(args)))
	}
	if query.UserID != nil {
		add("user_id = $%d", *query.UserID)
	}
	if query.APIKeyID != nil {
		add("api_key_id = $%d", *query.APIKeyID)
	}
	if query.Model != "" {
		add("model = $%d", query.Model)
	}
	if query.Protocol != "" {
		add("protocol = $%d", query.Protocol)
	}
	if query.StartTime != nil {
		add("completed_at >= $%d", query.StartTime.UTC())
	}
	if query.EndTime != nil {
		add("completed_at <= $%d", query.EndTime.UTC())
	}
	whereSQL := " WHERE " + strings.Join(where, " AND ")
	var total int64
	if err := s.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM conversation_archives"+whereSQL, args...).Scan(&total); err != nil {
		return archivePage{}, err
	}
	args = append(args, query.PageSize, (query.Page-1)*query.PageSize)
	rows, err := s.db.QueryContext(ctx, `
SELECT id, user_id, api_key_id, client_request_id::text, request_id, protocol, endpoint, model,
       started_at, completed_at, duration_ms, response_status, privacy_entities, expires_at
FROM conversation_archives`+whereSQL+fmt.Sprintf(`
ORDER BY completed_at DESC, id DESC
LIMIT $%d OFFSET $%d`, len(args)-1, len(args)), args...)
	if err != nil {
		return archivePage{}, err
	}
	defer func() { _ = rows.Close() }()
	items := make([]*conversationArchive, 0, query.PageSize)
	for rows.Next() {
		item, err := scanConversationArchive(rows, false)
		if err != nil {
			return archivePage{}, err
		}
		items = append(items, item)
	}
	if err := rows.Err(); err != nil {
		return archivePage{}, err
	}
	pages := int((total + int64(query.PageSize) - 1) / int64(query.PageSize))
	if pages < 1 {
		pages = 1
	}
	return archivePage{Items: items, Total: total, Page: query.Page, PageSize: query.PageSize, Pages: pages}, nil
}

func (s *postgresArchiveStore) GetArchive(ctx context.Context, id int64) (*conversationArchive, error) {
	row := s.db.QueryRowContext(ctx, `
SELECT id, user_id, api_key_id, client_request_id::text, request_id, protocol, endpoint, model,
       request_payload, assistant_text, tool_calls, final_response,
       started_at, completed_at, duration_ms, response_status, privacy_entities, expires_at
FROM conversation_archives
WHERE id = $1`, id)
	return scanConversationArchive(row, true)
}

type archiveScanner interface {
	Scan(...any) error
}

func scanConversationArchive(scanner archiveScanner, includePayload bool) (*conversationArchive, error) {
	item := &conversationArchive{}
	var clientRequestID sql.NullString
	var requestPayload, toolCalls, finalResponse []byte
	args := []any{
		&item.ID, &item.UserID, &item.APIKeyID, &clientRequestID, &item.RequestID,
		&item.Protocol, &item.Endpoint, &item.Model,
	}
	if includePayload {
		args = append(args, &requestPayload, &item.AssistantText, &toolCalls, &finalResponse)
	}
	args = append(args, &item.StartedAt, &item.CompletedAt, &item.DurationMS,
		&item.ResponseStatus, &item.PrivacyEntities, &item.ExpiresAt)
	if err := scanner.Scan(args...); err != nil {
		return nil, err
	}
	if clientRequestID.Valid {
		value := clientRequestID.String
		item.ClientRequestID = &value
	}
	if includePayload {
		item.RequestPayload = json.RawMessage(requestPayload)
		item.ToolCalls = json.RawMessage(toolCalls)
		item.FinalResponse = json.RawMessage(finalResponse)
	}
	return item, nil
}

func (s *postgresArchiveStore) CheckCompatibility(ctx context.Context) error {
	var archiveTable, pendingTable, usageTable, resolver bool
	var trajectoryTable, eventTable, batchTable, labelTable, emailResolver bool
	var keywordSnapshotTable bool
	err := s.db.QueryRowContext(ctx, `
SELECT
  to_regclass('public.conversation_archives') IS NOT NULL,
  to_regclass('public.conversation_archive_pending') IS NOT NULL,
  to_regclass('public.usage_logs') IS NOT NULL,
  to_regprocedure('public.resolve_archive_identity(uuid,timestamptz,timestamptz)') IS NOT NULL,
  to_regclass('public.agent_trajectories') IS NOT NULL,
  to_regclass('public.agent_trajectory_events') IS NOT NULL,
  to_regclass('public.agent_trajectory_import_batches') IS NOT NULL,
  to_regclass('public.agent_trajectory_labels') IS NOT NULL,
	  to_regprocedure('public.resolve_archive_user_by_email(text)') IS NOT NULL,
	  to_regclass('public.conversation_archive_keyword_snapshots') IS NOT NULL`).Scan(
		&archiveTable, &pendingTable, &usageTable, &resolver,
		&trajectoryTable, &eventTable, &batchTable, &labelTable, &emailResolver, &keywordSnapshotTable)
	if err != nil {
		return err
	}
	if !archiveTable || !pendingTable || !usageTable || !resolver ||
		!trajectoryTable || !eventTable || !batchTable || !labelTable || !emailResolver ||
		!keywordSnapshotTable {
		return fmt.Errorf("archive plugin database migration is incomplete")
	}
	var archiveColumns int
	if err := s.db.QueryRowContext(ctx, `
SELECT COUNT(*)
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'conversation_archives'
  AND column_name IN (
    'id', 'user_id', 'api_key_id', 'client_request_id', 'request_id',
    'protocol', 'endpoint', 'model', 'request_payload', 'assistant_text',
    'tool_calls', 'final_response', 'started_at', 'completed_at',
    'duration_ms', 'response_status', 'privacy_entities', 'expires_at'
  )`).Scan(&archiveColumns); err != nil {
		return err
	}
	if archiveColumns != 18 {
		return fmt.Errorf("conversation_archives compatibility check failed")
	}
	var pendingColumns int
	if err := s.db.QueryRowContext(ctx, `
SELECT COUNT(*)
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'conversation_archive_pending'
  AND column_name IN (
    'id', 'client_request_id', 'request_id', 'protocol', 'endpoint', 'model',
    'request_payload', 'assistant_text', 'tool_calls', 'final_response',
    'started_at', 'completed_at', 'duration_ms', 'response_status',
    'privacy_entities', 'expires_at', 'attempt_count', 'next_attempt_at',
    'deadline_at', 'last_error', 'created_at'
  )`).Scan(&pendingColumns); err != nil {
		return err
	}
	if pendingColumns != 21 {
		return fmt.Errorf("conversation_archive_pending compatibility check failed")
	}
	var resolverRows int
	if err := s.db.QueryRowContext(ctx, `
SELECT COUNT(*)
FROM resolve_archive_identity(
  '00000000-0000-0000-0000-000000000000'::uuid,
  NOW(),
  NOW()
)`).Scan(&resolverRows); err != nil {
		return fmt.Errorf("archive identity resolver permission check failed: %w", err)
	}
	return nil
}

func (s *postgresArchiveStore) Ping(ctx context.Context) error {
	return s.db.PingContext(ctx)
}

func (s *postgresArchiveStore) Close() error {
	return s.db.Close()
}

type pendingArchive struct {
	server *proxyServer

	clientRequestID   string
	requestID         string
	protocol          streamProtocol
	endpoint          string
	model             string
	requestPayload    json.RawMessage
	startedAt         time.Time
	responseStatus    int
	requestIsStream   bool
	eligible          bool
	requestContext    context.Context
	clientWriteFailed atomic.Bool

	parser      *sseParser
	accumulator *conversationAccumulator
	finishOnce  sync.Once
	discardOnce sync.Once
}

func newPendingArchive(server *proxyServer, req *http.Request, payload []byte, protocol streamProtocol) *pendingArchive {
	model, stream := requestMetadata(payload, req.URL.Path, protocol)
	archivePayload, err := compactArchiveRequest(payload, protocol)
	if err != nil {
		// Never fall back to the original request: it may contain full history or
		// media bytes. A minimal marker is safer than retaining prohibited data.
		log.Printf(`{"event":"archive_request_compaction_failed","protocol":%q,"error":%q}`,
			protocol.String(), err.Error())
		archivePayload = emptyCompactArchiveRequest(protocol)
	}
	record := &pendingArchive{
		server: server, requestID: randomRequestID(), protocol: protocol,
		endpoint: req.URL.Path, model: model, requestPayload: archivePayload,
		startedAt: time.Now().UTC(), requestIsStream: stream,
		requestContext: req.Context(), accumulator: newConversationAccumulator(protocol),
	}
	record.parser = newSSEParser(record.accumulator.consume)
	return record
}

func requestMetadata(payload []byte, path string, protocol streamProtocol) (string, bool) {
	var body map[string]any
	if json.Unmarshal(payload, &body) != nil {
		return "", false
	}
	model, _ := body["model"].(string)
	stream, _ := body["stream"].(bool)
	if protocol == protocolGemini {
		stream = true
		if model == "" {
			model = geminiModelFromPath(path)
		}
	}
	return model, stream
}

func geminiModelFromPath(path string) string {
	const marker = "/models/"
	index := strings.Index(path, marker)
	if index < 0 {
		return ""
	}
	model := path[index+len(marker):]
	if separator := strings.IndexByte(model, ':'); separator >= 0 {
		model = model[:separator]
	}
	return strings.TrimSpace(model)
}

func randomRequestID() string {
	random := make([]byte, 16)
	if _, err := rand.Read(random); err != nil {
		return fmt.Sprintf("archive-%d", time.Now().UnixNano())
	}
	return "archive-" + hex.EncodeToString(random)
}

func (r *pendingArchive) discard(reason string) {
	r.discardOnce.Do(func() {
		r.server.metrics.discardedStreams.Add(1)
		log.Printf(`{"event":"archive_stream_discarded","request_id":%q,"protocol":%q,"reason":%q}`,
			r.requestID, r.protocol.String(), reason)
	})
}

func (r *pendingArchive) finish(eof bool, closeErr error) {
	r.finishOnce.Do(func() {
		if !r.eligible || !eof || closeErr != nil {
			r.discard("client_or_upstream_incomplete")
			return
		}
		if r.clientWriteFailed.Load() || (r.requestContext != nil && r.requestContext.Err() != nil) {
			r.discard("client_stream_delivery_failed")
			return
		}
		r.parser.Finish()
		if !r.accumulator.complete {
			r.discard("protocol_completion_missing")
			return
		}
		assistantText := r.accumulator.text.String()
		if strings.TrimSpace(assistantText) == "" {
			r.discard("empty_assistant_text")
			return
		}

		completedAt := time.Now().UTC()
		toolCalls, finalResponse, err := r.accumulator.result(r.model)
		if err != nil {
			r.discard("final_result_encoding_failed")
			return
		}
		item := completedArchive{
			ClientRequestID: r.clientRequestID,
			RequestID:       "client:" + r.clientRequestID,
			Protocol:        r.protocol.String(),
			Endpoint:        r.endpoint,
			Model:           r.model,
			RequestPayload:  r.requestPayload,
			AssistantText:   assistantText,
			ToolCalls:       toolCalls,
			FinalResponse:   finalResponse,
			StartedAt:       r.startedAt,
			CompletedAt:     completedAt,
			DurationMS:      completedAt.Sub(r.startedAt).Milliseconds(),
			ResponseStatus:  r.responseStatus,
			ExpiresAt:       completedAt.Add(r.server.cfg.Retention),
			DeadlineAt:      completedAt.Add(r.server.cfg.IdentityResolveTimeout),
		}
		privacyCtx, privacyCancel := context.WithTimeout(context.Background(), r.server.cfg.PrivacyTimeout)
		redactedItem, err := r.server.privacy.redactArchive(privacyCtx, item)
		privacyCancel()
		if err != nil {
			r.server.metrics.privacyFailures.Add(1)
			log.Printf(`{"event":"privacy_filter_failed","request_id":%q,"client_request_id":%q,"stage":"archive_write","error":%q}`,
				r.requestID, r.clientRequestID, err.Error())
			return
		}
		item = redactedItem
		ctx, cancel := context.WithTimeout(context.Background(), r.server.cfg.DatabaseTimeout)
		defer cancel()
		queued, err := r.server.store.QueueCompleted(ctx, item)
		if err != nil {
			r.server.metrics.insertFailures.Add(1)
			log.Printf(`{"event":"archive_queue_failed","request_id":%q,"client_request_id":%q,"error":%q}`,
				r.requestID, r.clientRequestID, err.Error())
			return
		}
		if queued.Queued {
			r.server.metrics.queued.Add(1)
		}
		if queued.Inserted {
			r.server.metrics.inserted.Add(1)
			log.Printf(`{"event":"archive_inserted","request_id":%q,"client_request_id":%q,"protocol":%q}`,
				item.RequestID, item.ClientRequestID, item.Protocol)
		} else if queued.Queued {
			log.Printf(`{"event":"archive_identity_pending","request_id":%q,"client_request_id":%q}`,
				item.RequestID, item.ClientRequestID)
			r.server.wakeResolver()
		}
	})
}

type archiveResponseBody struct {
	io.ReadCloser
	record *pendingArchive
	eof    bool
}

func (b *archiveResponseBody) Read(buffer []byte) (int, error) {
	n, err := b.ReadCloser.Read(buffer)
	if n > 0 {
		b.record.parser.Write(buffer[:n])
	}
	if err == io.EOF {
		b.eof = true
	}
	return n, err
}

func (b *archiveResponseBody) Close() error {
	err := b.ReadCloser.Close()
	b.record.finish(b.eof, err)
	return err
}
