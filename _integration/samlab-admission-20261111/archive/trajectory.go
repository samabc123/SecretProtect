package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
)

const trajectorySchemaVersion = "codex-agent-trajectory/1.0"

type archiveUser struct {
	UserID int64  `json:"user_id"`
	Email  string `json:"email"`
	Role   string `json:"role"`
}

type trajectoryManifest struct {
	RecordType    string `json:"record_type"`
	SchemaVersion string `json:"schema_version"`
	BatchID       string `json:"batch_id"`
	ExportedAt    string `json:"exported_at"`
	Source        string `json:"source"`
	Profile       string `json:"profile"`
	RetentionDays int    `json:"retention_days"`
}

type trajectoryMessageExtra struct {
	EventType       string `json:"event_type"`
	Phase           string `json:"phase,omitempty"`
	SourceItemID    string `json:"source_item_id,omitempty"`
	Status          string `json:"status,omitempty"`
	ToolName        string `json:"tool_name,omitempty"`
	ToolCallID      string `json:"tool_call_id,omitempty"`
	OriginalBytes   int64  `json:"original_bytes,omitempty"`
	StoredBytes     int64  `json:"stored_bytes,omitempty"`
	Truncated       bool   `json:"truncated,omitempty"`
	ContentSHA256   string `json:"content_sha256"`
	IsFinal         bool   `json:"is_final,omitempty"`
	PayloadOmitted  bool   `json:"payload_omitted,omitempty"`
	PrivacyEntities int    `json:"privacy_entities,omitempty"`
	LegacySource    string `json:"source,omitempty"`
	LegacyKind      string `json:"kind,omitempty"`
}

type trajectoryMessage struct {
	Sequence   int                    `json:"seq"`
	Role       string                 `json:"role"`
	Content    json.RawMessage        `json:"content"`
	Timestamp  string                 `json:"timestamp"`
	Extra      trajectoryMessageExtra `json:"extra"`
	ToolCalls  json.RawMessage        `json:"tool_calls,omitempty"`
	ToolCallID string                 `json:"tool_call_id,omitempty"`
	Name       string                 `json:"name,omitempty"`
}

type trajectoryMetrics struct {
	EventCount      int `json:"event_count"`
	ToolCallCount   int `json:"tool_call_count"`
	PrivacyEntities int `json:"privacy_entities"`
}

type trajectoryImportRecord struct {
	RecordType      string              `json:"record_type"`
	SchemaVersion   string              `json:"schema_version"`
	TrajectoryID    string              `json:"trajectory_id"`
	Source          string              `json:"source"`
	SourceThreadID  string              `json:"source_thread_id"`
	SourceTurnID    string              `json:"source_turn_id"`
	Title           string              `json:"title"`
	Model           string              `json:"model"`
	ModelProvider   string              `json:"model_provider"`
	Status          string              `json:"status"`
	StartedAt       string              `json:"started_at"`
	CompletedAt     string              `json:"completed_at"`
	DurationMS      int64               `json:"duration_ms"`
	ExpiresAt       string              `json:"expires_at"`
	Messages        []trajectoryMessage `json:"messages"`
	FinalText       string              `json:"final_text"`
	Metrics         trajectoryMetrics   `json:"metrics"`
	ContentSHA256   string              `json:"content_sha256"`
	PrivacyEntities int                 `json:"-"`
}

type trajectoryBatchStart struct {
	ID             string
	User           archiveUser
	Source         string
	SchemaVersion  string
	ManifestSHA256 string
}

type trajectoryBatchState struct {
	ID               string
	AlreadyCompleted bool
}

type trajectoryImportStats struct {
	Total    int `json:"total_records"`
	Inserted int `json:"inserted_records"`
	Updated  int `json:"updated_records"`
	Skipped  int `json:"skipped_records"`
	Failed   int `json:"failed_records"`
}

type trajectoryQuery struct {
	Page      int
	PageSize  int
	UserID    *int64
	Status    string
	Model     string
	Source    string
	ToolName  string
	StartTime *time.Time
	EndTime   *time.Time
}

type agentTrajectory struct {
	ID              int64           `json:"id"`
	ImportBatchID   *string         `json:"import_batch_id,omitempty"`
	UserID          int64           `json:"user_id"`
	Source          string          `json:"source"`
	SourceThreadID  string          `json:"source_thread_id"`
	SourceTurnID    string          `json:"source_turn_id"`
	TrajectoryID    string          `json:"trajectory_id"`
	Title           string          `json:"title"`
	Model           string          `json:"model"`
	ModelProvider   string          `json:"model_provider"`
	Status          string          `json:"status"`
	FinalText       string          `json:"final_text,omitempty"`
	StartedAt       time.Time       `json:"started_at"`
	CompletedAt     time.Time       `json:"completed_at"`
	DurationMS      int64           `json:"duration_ms"`
	EventCount      int             `json:"event_count"`
	ToolCallCount   int             `json:"tool_call_count"`
	PrivacyEntities int             `json:"privacy_entities"`
	ContentSHA256   string          `json:"content_sha256"`
	Metadata        json.RawMessage `json:"metadata,omitempty"`
	ExpiresAt       time.Time       `json:"expires_at"`
}

type trajectoryPage struct {
	Items    []*agentTrajectory `json:"items"`
	Total    int64              `json:"total"`
	Page     int                `json:"page"`
	PageSize int                `json:"page_size"`
	Pages    int                `json:"pages"`
}

type agentTrajectoryEvent struct {
	ID              int64           `json:"id"`
	TrajectoryID    int64           `json:"trajectory_id"`
	SequenceNo      int             `json:"sequence_no"`
	SourceItemID    *string         `json:"source_item_id,omitempty"`
	EventType       string          `json:"event_type"`
	Role            *string         `json:"role,omitempty"`
	Phase           *string         `json:"phase,omitempty"`
	ToolCallID      *string         `json:"tool_call_id,omitempty"`
	ToolName        *string         `json:"tool_name,omitempty"`
	Content         json.RawMessage `json:"content"`
	Status          *string         `json:"status,omitempty"`
	EventAt         time.Time       `json:"event_at"`
	PrivacyEntities int             `json:"privacy_entities"`
	OriginalBytes   int64           `json:"original_bytes"`
	StoredBytes     int64           `json:"stored_bytes"`
	IsTruncated     bool            `json:"is_truncated"`
	ContentSHA256   string          `json:"content_sha256"`
	Extra           json.RawMessage `json:"extra"`
}

type trajectoryEventPage struct {
	Items    []*agentTrajectoryEvent `json:"items"`
	Total    int64                   `json:"total"`
	Page     int                     `json:"page"`
	PageSize int                     `json:"page_size"`
	Pages    int                     `json:"pages"`
}

func (s *postgresArchiveStore) ResolveArchiveUserByEmail(ctx context.Context, email string) (archiveUser, error) {
	user := archiveUser{}
	err := s.db.QueryRowContext(ctx, `
SELECT user_id, email, role
FROM resolve_archive_user_by_email($1)`, strings.TrimSpace(email)).Scan(&user.UserID, &user.Email, &user.Role)
	if errors.Is(err, sql.ErrNoRows) {
		return archiveUser{}, fmt.Errorf("active Sub2API email was not found")
	}
	return user, err
}

func (s *postgresArchiveStore) StartTrajectoryBatch(ctx context.Context, item trajectoryBatchStart) (trajectoryBatchState, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return trajectoryBatchState{}, err
	}
	defer func() { _ = tx.Rollback() }()
	var existingID, status, existingHash, existingSource, existingSchema string
	var existingUserID int64
	err = tx.QueryRowContext(ctx, `
SELECT id::text, user_id, status, manifest_sha256, source, schema_version
FROM agent_trajectory_import_batches
WHERE id = $1::uuid OR (user_id = $2 AND manifest_sha256 = $3)
ORDER BY CASE WHEN id = $1::uuid THEN 0 ELSE 1 END
LIMIT 1
FOR UPDATE`, item.ID, item.User.UserID, item.ManifestSHA256).Scan(
		&existingID, &existingUserID, &status, &existingHash, &existingSource, &existingSchema)
	if err == nil {
		if existingUserID != item.User.UserID || existingHash != item.ManifestSHA256 ||
			existingSource != item.Source || existingSchema != item.SchemaVersion {
			return trajectoryBatchState{}, fmt.Errorf("trajectory import batch identity collision")
		}
		if status == "completed" {
			return trajectoryBatchState{ID: existingID, AlreadyCompleted: true}, tx.Commit()
		}
		_, err = tx.ExecContext(ctx, `
UPDATE agent_trajectory_import_batches
SET status = 'importing', total_records = 0, inserted_records = 0,
    updated_records = 0, skipped_records = 0, failed_records = 0,
    error_summary = '', completed_at = NULL
WHERE id = $1::uuid`, existingID)
		if err != nil {
			return trajectoryBatchState{}, err
		}
		return trajectoryBatchState{ID: existingID}, tx.Commit()
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return trajectoryBatchState{}, err
	}
	_, err = tx.ExecContext(ctx, `
INSERT INTO agent_trajectory_import_batches (
  id, user_id, email_snapshot, source, schema_version, manifest_sha256, status
) VALUES ($1::uuid, $2, $3, $4, $5, $6, 'importing')`,
		item.ID, item.User.UserID, item.User.Email, item.Source, item.SchemaVersion, item.ManifestSHA256)
	if err != nil {
		return trajectoryBatchState{}, err
	}
	return trajectoryBatchState{ID: item.ID}, tx.Commit()
}

func (s *postgresArchiveStore) ImportTrajectory(ctx context.Context, batchID string, userID int64, record trajectoryImportRecord) (string, error) {
	startedAt, err := time.Parse(time.RFC3339, record.StartedAt)
	if err != nil {
		return "", err
	}
	completedAt, err := time.Parse(time.RFC3339, record.CompletedAt)
	if err != nil {
		return "", err
	}
	expiresAt := completedAt.Add(30 * 24 * time.Hour)
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return "", err
	}
	defer func() { _ = tx.Rollback() }()

	var trajectoryID int64
	var existingStatus, existingHash string
	err = tx.QueryRowContext(ctx, `
SELECT id, status, content_sha256
FROM agent_trajectories
WHERE user_id = $1 AND source = $2 AND source_thread_id = $3 AND source_turn_id = $4
FOR UPDATE`, userID, record.Source, record.SourceThreadID, record.SourceTurnID).Scan(
		&trajectoryID, &existingStatus, &existingHash)
	action := "inserted"
	if errors.Is(err, sql.ErrNoRows) {
		metadata, _ := json.Marshal(map[string]any{"schema_version": record.SchemaVersion})
		err = tx.QueryRowContext(ctx, `
INSERT INTO agent_trajectories (
  import_batch_id, user_id, source, source_thread_id, source_turn_id, trajectory_id,
  title, model, model_provider, status, final_text, started_at, completed_at,
  duration_ms, event_count, tool_call_count, privacy_entities, content_sha256,
  metadata, expires_at
) VALUES (
  $1::uuid, $2, $3, $4, $5, $6,
  $7, $8, $9, $10, $11, $12, $13,
  $14, $15, $16, $17, $18, $19, $20
) RETURNING id`, batchID, userID, record.Source, record.SourceThreadID, record.SourceTurnID,
			record.TrajectoryID, record.Title, record.Model, record.ModelProvider, record.Status,
			record.FinalText, startedAt, completedAt, record.DurationMS, len(record.Messages),
			record.Metrics.ToolCallCount, record.PrivacyEntities, record.ContentSHA256,
			metadata, expiresAt).Scan(&trajectoryID)
		if err != nil {
			return "", err
		}
	} else if err != nil {
		return "", err
	} else {
		if existingStatus != "incomplete" || (existingHash == record.ContentSHA256 && record.Status == "incomplete") {
			return "skipped", tx.Commit()
		}
		action = "updated"
		if _, err := tx.ExecContext(ctx, `DELETE FROM agent_trajectory_events WHERE trajectory_id = $1`, trajectoryID); err != nil {
			return "", err
		}
		_, err = tx.ExecContext(ctx, `
UPDATE agent_trajectories
SET import_batch_id = $2::uuid, trajectory_id = $3, title = $4, model = $5,
    model_provider = $6, status = $7, final_text = $8, started_at = $9,
    completed_at = $10, duration_ms = $11, event_count = $12,
    tool_call_count = $13, privacy_entities = $14, content_sha256 = $15,
    expires_at = $16, updated_at = NOW()
WHERE id = $1`, trajectoryID, batchID, record.TrajectoryID, record.Title, record.Model,
			record.ModelProvider, record.Status, record.FinalText, startedAt, completedAt,
			record.DurationMS, len(record.Messages), record.Metrics.ToolCallCount,
			record.PrivacyEntities, record.ContentSHA256, expiresAt)
		if err != nil {
			return "", err
		}
	}

	for _, message := range record.Messages {
		eventAt, err := time.Parse(time.RFC3339, message.Timestamp)
		if err != nil {
			return "", err
		}
		content := map[string]any{"content": json.RawMessage(message.Content)}
		if len(message.ToolCalls) > 0 {
			content["tool_calls"] = json.RawMessage(message.ToolCalls)
		}
		if message.ToolCallID != "" {
			content["tool_call_id"] = message.ToolCallID
		}
		contentJSON, err := json.Marshal(content)
		if err != nil {
			return "", err
		}
		extraJSON, _ := json.Marshal(message.Extra)
		_, err = tx.ExecContext(ctx, `
INSERT INTO agent_trajectory_events (
  trajectory_id, sequence_no, source_item_id, event_type, role, phase,
  tool_call_id, tool_name, content, status, event_at, privacy_entities,
  original_bytes, stored_bytes, is_truncated, content_sha256, extra
) VALUES (
  $1, $2, NULLIF($3, ''), $4, NULLIF($5, ''), NULLIF($6, ''),
  NULLIF($7, ''), NULLIF($8, ''), $9, NULLIF($10, ''), $11, $12,
  $13, $14, $15, $16, $17
)`, trajectoryID, message.Sequence, message.Extra.SourceItemID, message.Extra.EventType,
			message.Role, message.Extra.Phase, firstNonEmpty(message.Extra.ToolCallID, message.ToolCallID),
			firstNonEmpty(message.Extra.ToolName, message.Name), contentJSON, message.Extra.Status,
			eventAt, message.Extra.PrivacyEntities, message.Extra.OriginalBytes,
			message.Extra.StoredBytes, message.Extra.Truncated, message.Extra.ContentSHA256, extraJSON)
		if err != nil {
			return "", err
		}
	}
	return action, tx.Commit()
}

func (s *postgresArchiveStore) FinishTrajectoryBatch(ctx context.Context, batchID string, stats trajectoryImportStats, errorSummary string) error {
	status := "completed"
	if stats.Failed > 0 {
		status = "partial"
	}
	_, err := s.db.ExecContext(ctx, `
UPDATE agent_trajectory_import_batches
SET status = $2, total_records = $3, inserted_records = $4,
    updated_records = $5, skipped_records = $6, failed_records = $7,
    error_summary = $8, completed_at = NOW()
WHERE id = $1::uuid`, batchID, status, stats.Total, stats.Inserted,
		stats.Updated, stats.Skipped, stats.Failed, truncateError(errorSummary, 2000))
	return err
}

func (s *postgresArchiveStore) ListTrajectories(ctx context.Context, query trajectoryQuery) (trajectoryPage, error) {
	where := []string{"1=1"}
	args := make([]any, 0, 10)
	add := func(clause string, value any) {
		args = append(args, value)
		where = append(where, fmt.Sprintf(clause, len(args)))
	}
	if query.UserID != nil {
		add("t.user_id = $%d", *query.UserID)
	}
	if query.Status != "" {
		add("t.status = $%d", query.Status)
	}
	if query.Model != "" {
		add("t.model = $%d", query.Model)
	}
	if query.Source != "" {
		add("t.source = $%d", query.Source)
	}
	if query.StartTime != nil {
		add("t.completed_at >= $%d", query.StartTime.UTC())
	}
	if query.EndTime != nil {
		add("t.completed_at <= $%d", query.EndTime.UTC())
	}
	if query.ToolName != "" {
		args = append(args, query.ToolName)
		where = append(where, fmt.Sprintf("EXISTS (SELECT 1 FROM agent_trajectory_events e WHERE e.trajectory_id = t.id AND e.tool_name = $%d)", len(args)))
	}
	whereSQL := " WHERE " + strings.Join(where, " AND ")
	var total int64
	if err := s.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM agent_trajectories t"+whereSQL, args...).Scan(&total); err != nil {
		return trajectoryPage{}, err
	}
	args = append(args, query.PageSize, (query.Page-1)*query.PageSize)
	rows, err := s.db.QueryContext(ctx, `
SELECT t.id, t.import_batch_id::text, t.user_id, t.source, t.source_thread_id,
       t.source_turn_id, t.trajectory_id, t.title, t.model, t.model_provider,
       t.status, t.started_at, t.completed_at, t.duration_ms, t.event_count,
       t.tool_call_count, t.privacy_entities, t.content_sha256, t.expires_at
FROM agent_trajectories t`+whereSQL+fmt.Sprintf(" ORDER BY t.completed_at DESC, t.id DESC LIMIT $%d OFFSET $%d", len(args)-1, len(args)), args...)
	if err != nil {
		return trajectoryPage{}, err
	}
	defer func() { _ = rows.Close() }()
	items := make([]*agentTrajectory, 0, query.PageSize)
	for rows.Next() {
		item := &agentTrajectory{}
		var batchID sql.NullString
		if err := rows.Scan(&item.ID, &batchID, &item.UserID, &item.Source, &item.SourceThreadID,
			&item.SourceTurnID, &item.TrajectoryID, &item.Title, &item.Model, &item.ModelProvider,
			&item.Status, &item.StartedAt, &item.CompletedAt, &item.DurationMS, &item.EventCount,
			&item.ToolCallCount, &item.PrivacyEntities, &item.ContentSHA256, &item.ExpiresAt); err != nil {
			return trajectoryPage{}, err
		}
		if batchID.Valid {
			item.ImportBatchID = &batchID.String
		}
		items = append(items, item)
	}
	pages := 0
	if total > 0 {
		pages = int((total + int64(query.PageSize) - 1) / int64(query.PageSize))
	}
	return trajectoryPage{Items: items, Total: total, Page: query.Page, PageSize: query.PageSize, Pages: pages}, rows.Err()
}

func (s *postgresArchiveStore) GetTrajectory(ctx context.Context, id int64) (*agentTrajectory, error) {
	item := &agentTrajectory{}
	var batchID sql.NullString
	var metadata []byte
	err := s.db.QueryRowContext(ctx, `
SELECT id, import_batch_id::text, user_id, source, source_thread_id, source_turn_id,
       trajectory_id, title, model, model_provider, status, final_text, started_at,
       completed_at, duration_ms, event_count, tool_call_count, privacy_entities,
       content_sha256, metadata, expires_at
FROM agent_trajectories WHERE id = $1`, id).Scan(
		&item.ID, &batchID, &item.UserID, &item.Source, &item.SourceThreadID, &item.SourceTurnID,
		&item.TrajectoryID, &item.Title, &item.Model, &item.ModelProvider, &item.Status,
		&item.FinalText, &item.StartedAt, &item.CompletedAt, &item.DurationMS, &item.EventCount,
		&item.ToolCallCount, &item.PrivacyEntities, &item.ContentSHA256, &metadata, &item.ExpiresAt)
	if err != nil {
		return nil, err
	}
	if batchID.Valid {
		item.ImportBatchID = &batchID.String
	}
	item.Metadata = json.RawMessage(metadata)
	return item, nil
}

func (s *postgresArchiveStore) ListTrajectoryEvents(ctx context.Context, trajectoryID int64, page, pageSize int) (trajectoryEventPage, error) {
	var total int64
	if err := s.db.QueryRowContext(ctx, `SELECT COUNT(*) FROM agent_trajectory_events WHERE trajectory_id = $1`, trajectoryID).Scan(&total); err != nil {
		return trajectoryEventPage{}, err
	}
	rows, err := s.db.QueryContext(ctx, `
SELECT id, trajectory_id, sequence_no, source_item_id, event_type, role, phase,
       tool_call_id, tool_name, content, status, event_at, privacy_entities,
       original_bytes, stored_bytes, is_truncated, content_sha256, extra
FROM agent_trajectory_events
WHERE trajectory_id = $1
ORDER BY sequence_no
LIMIT $2 OFFSET $3`, trajectoryID, pageSize, (page-1)*pageSize)
	if err != nil {
		return trajectoryEventPage{}, err
	}
	defer func() { _ = rows.Close() }()
	items := make([]*agentTrajectoryEvent, 0, pageSize)
	for rows.Next() {
		item := &agentTrajectoryEvent{}
		var sourceItemID, role, phase, toolCallID, toolName, status sql.NullString
		var content, extra []byte
		if err := rows.Scan(&item.ID, &item.TrajectoryID, &item.SequenceNo, &sourceItemID,
			&item.EventType, &role, &phase, &toolCallID, &toolName, &content, &status,
			&item.EventAt, &item.PrivacyEntities, &item.OriginalBytes, &item.StoredBytes,
			&item.IsTruncated, &item.ContentSHA256, &extra); err != nil {
			return trajectoryEventPage{}, err
		}
		if sourceItemID.Valid {
			item.SourceItemID = &sourceItemID.String
		}
		if role.Valid {
			item.Role = &role.String
		}
		if phase.Valid {
			item.Phase = &phase.String
		}
		if toolCallID.Valid {
			item.ToolCallID = &toolCallID.String
		}
		if toolName.Valid {
			item.ToolName = &toolName.String
		}
		if status.Valid {
			item.Status = &status.String
		}
		item.Content = json.RawMessage(content)
		item.Extra = json.RawMessage(extra)
		items = append(items, item)
	}
	pages := 0
	if total > 0 {
		pages = int((total + int64(pageSize) - 1) / int64(pageSize))
	}
	return trajectoryEventPage{Items: items, Total: total, Page: page, PageSize: pageSize, Pages: pages}, rows.Err()
}
