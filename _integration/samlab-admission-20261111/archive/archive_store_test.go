package main

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"io"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/lib/pq"
)

type archiveIdentityFixture int

const (
	archiveIdentityMissing archiveIdentityFixture = iota
	archiveIdentityUnique
	archiveIdentityAmbiguous
	archiveIdentityFailure
)

type archiveStoreTestConnector struct {
	conn *archiveStoreTestConn
}

func (c *archiveStoreTestConnector) Connect(context.Context) (driver.Conn, error) {
	return c.conn, nil
}

func (c *archiveStoreTestConnector) Driver() driver.Driver {
	return archiveStoreTestDriver{conn: c.conn}
}

type archiveStoreTestDriver struct {
	conn *archiveStoreTestConn
}

func (d archiveStoreTestDriver) Open(string) (driver.Conn, error) {
	return d.conn, nil
}

type archiveStoreTestConn struct {
	mu sync.Mutex

	identity archiveIdentityFixture
	item     completedArchive
	pending  bool
	archived bool

	beginCount    int
	commitCount   int
	rollbackCount int
	resolveCount  int
	retryCount    int
	lastError     string
	calls         []string
}

func (c *archiveStoreTestConn) Prepare(string) (driver.Stmt, error) {
	return nil, errors.New("prepared statements are not supported by the test driver")
}

func (c *archiveStoreTestConn) Close() error { return nil }

func (c *archiveStoreTestConn) Begin() (driver.Tx, error) {
	return c.BeginTx(context.Background(), driver.TxOptions{})
}

func (c *archiveStoreTestConn) BeginTx(context.Context, driver.TxOptions) (driver.Tx, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.beginCount++
	c.calls = append(c.calls, "begin")
	return &archiveStoreTestTx{conn: c}, nil
}

func (c *archiveStoreTestConn) ExecContext(
	_ context.Context,
	query string,
	args []driver.NamedValue,
) (driver.Result, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	normalized := strings.Join(strings.Fields(query), " ")
	c.calls = append(c.calls, "exec:"+normalized)
	switch {
	case strings.Contains(normalized, "DELETE FROM conversation_archives"):
		return driver.RowsAffected(0), nil
	case strings.Contains(normalized, "DELETE FROM agent_trajectories"):
		return driver.RowsAffected(0), nil
	case strings.Contains(normalized, "DELETE FROM agent_trajectory_import_batches"):
		return driver.RowsAffected(0), nil
	case strings.Contains(normalized, "INSERT INTO conversation_archive_pending"):
		if c.pending {
			return driver.RowsAffected(0), nil
		}
		c.pending = true
		return driver.RowsAffected(1), nil
	case strings.Contains(normalized, "INSERT INTO conversation_archives"):
		c.archived = true
		return driver.RowsAffected(1), nil
	case strings.Contains(normalized, "DELETE FROM conversation_archive_pending"):
		if !c.pending {
			return driver.RowsAffected(0), nil
		}
		c.pending = false
		return driver.RowsAffected(1), nil
	case strings.Contains(normalized, "UPDATE conversation_archive_pending"):
		if !c.pending {
			return driver.RowsAffected(0), nil
		}
		c.retryCount++
		if len(args) >= 3 {
			c.lastError, _ = args[2].Value.(string)
		}
		return driver.RowsAffected(1), nil
	default:
		return nil, errors.New("unexpected exec query: " + normalized)
	}
}

func (c *archiveStoreTestConn) QueryContext(
	_ context.Context,
	query string,
	_ []driver.NamedValue,
) (driver.Rows, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	normalized := strings.Join(strings.Fields(query), " ")
	c.calls = append(c.calls, "query:"+normalized)
	switch {
	case strings.Contains(normalized, "FROM conversation_archive_pending"):
		columns := []string{
			"client_request_id", "request_id", "protocol", "endpoint", "model",
			"request_payload", "assistant_text", "tool_calls", "final_response",
			"started_at", "completed_at", "duration_ms", "response_status",
			"privacy_entities", "expires_at", "attempt_count", "next_attempt_at", "deadline_at",
		}
		if !c.pending {
			return &archiveStoreTestRows{columns: columns}, nil
		}
		item := c.item
		return &archiveStoreTestRows{columns: columns, values: [][]driver.Value{{
			item.ClientRequestID, item.RequestID, item.Protocol, item.Endpoint, item.Model,
			[]byte(item.RequestPayload), item.AssistantText, []byte(item.ToolCalls), []byte(item.FinalResponse),
			item.StartedAt, item.CompletedAt, item.DurationMS, int64(item.ResponseStatus),
			int64(item.PrivacyEntities), item.ExpiresAt, int64(item.AttemptCount), item.NextAttemptAt, item.DeadlineAt,
		}}}, nil
	case strings.Contains(normalized, "FROM resolve_archive_identity"):
		c.resolveCount++
		switch c.identity {
		case archiveIdentityMissing:
			return &archiveStoreTestRows{columns: []string{"user_id", "api_key_id"}}, nil
		case archiveIdentityUnique:
			return &archiveStoreTestRows{
				columns: []string{"user_id", "api_key_id"},
				values:  [][]driver.Value{{int64(11), int64(22)}},
			}, nil
		case archiveIdentityAmbiguous:
			return nil, &pq.Error{
				Code: "P0001", Message: "ambiguous archive identity for client request",
				Detail: "archive_identity_ambiguous",
			}
		case archiveIdentityFailure:
			return nil, errors.New("temporary resolver failure")
		}
	}
	return nil, errors.New("unexpected query: " + normalized)
}

type archiveStoreTestTx struct {
	conn *archiveStoreTestConn
}

func (tx *archiveStoreTestTx) Commit() error {
	tx.conn.mu.Lock()
	defer tx.conn.mu.Unlock()
	tx.conn.commitCount++
	tx.conn.calls = append(tx.conn.calls, "commit")
	return nil
}

func (tx *archiveStoreTestTx) Rollback() error {
	tx.conn.mu.Lock()
	defer tx.conn.mu.Unlock()
	tx.conn.rollbackCount++
	tx.conn.calls = append(tx.conn.calls, "rollback")
	return nil
}

type archiveStoreTestRows struct {
	columns []string
	values  [][]driver.Value
	index   int
}

func (r *archiveStoreTestRows) Columns() []string { return r.columns }
func (r *archiveStoreTestRows) Close() error      { return nil }
func (r *archiveStoreTestRows) Next(destination []driver.Value) error {
	if r.index >= len(r.values) {
		return io.EOF
	}
	copy(destination, r.values[r.index])
	r.index++
	return nil
}

func newArchiveStoreTestFixture(identity archiveIdentityFixture, pending bool) (*postgresArchiveStore, *archiveStoreTestConn) {
	now := time.Date(2026, time.August, 5, 12, 0, 0, 0, time.UTC)
	item := completedArchive{
		ClientRequestID: "9f7307de-ea68-41ac-bdea-059098ae1a82",
		RequestID:       "client:9f7307de-ea68-41ac-bdea-059098ae1a82",
		Protocol:        "openai_chat_completions",
		Endpoint:        "/v1/chat/completions",
		Model:           "gpt-test",
		RequestPayload:  []byte(`{"model":"gpt-test","stream":true}`),
		AssistantText:   "complete answer",
		ToolCalls:       []byte(`[]`),
		FinalResponse:   []byte(`{"text":"complete answer"}`),
		StartedAt:       now.Add(-time.Second),
		CompletedAt:     now,
		DurationMS:      1000,
		ResponseStatus:  200,
		ExpiresAt:       now.Add(30 * 24 * time.Hour),
		NextAttemptAt:   now,
		DeadlineAt:      now.Add(30 * time.Second),
	}
	conn := &archiveStoreTestConn{identity: identity, item: item, pending: pending}
	db := sql.OpenDB(&archiveStoreTestConnector{conn: conn})
	db.SetMaxOpenConns(1)
	return &postgresArchiveStore{db: db}, conn
}

func TestQueueCompletedCommitsPendingBeforeIdentityResolution(t *testing.T) {
	store, fixture := newArchiveStoreTestFixture(archiveIdentityFailure, false)
	defer func() { _ = store.Close() }()
	result, err := store.QueueCompleted(context.Background(), fixture.item)
	if err != nil {
		t.Fatal(err)
	}
	if !result.Queued || result.Inserted {
		t.Fatalf("queue result=%+v", result)
	}
	if !fixture.pending || fixture.resolveCount != 0 || fixture.beginCount != 0 {
		t.Fatalf("pending=%v resolver_calls=%d transactions=%d", fixture.pending, fixture.resolveCount, fixture.beginCount)
	}
}

func TestDeleteExpiredCastsRetentionParameterAsTimestamp(t *testing.T) {
	store, fixture := newArchiveStoreTestFixture(archiveIdentityMissing, false)
	defer func() { _ = store.Close() }()

	deleted, err := store.DeleteExpired(context.Background(), fixture.item.CompletedAt, 100)
	if err != nil {
		t.Fatal(err)
	}
	if deleted != 0 {
		t.Fatalf("deleted=%d", deleted)
	}

	fixture.mu.Lock()
	defer fixture.mu.Unlock()
	for _, call := range fixture.calls {
		if strings.Contains(call, "DELETE FROM agent_trajectory_import_batches") {
			if !strings.Contains(call, "$1::TIMESTAMPTZ - INTERVAL '30 days'") {
				t.Fatalf("retention query lacks timestamp cast: %s", call)
			}
			return
		}
	}
	t.Fatal("agent trajectory import batch retention query was not executed")
}

func TestResolveDueKeepsPendingWhenIdentityIsNotYetAvailable(t *testing.T) {
	store, fixture := newArchiveStoreTestFixture(archiveIdentityMissing, true)
	defer func() { _ = store.Close() }()
	stats, err := store.ResolveDue(context.Background(), fixture.item.CompletedAt, 1)
	if err != nil {
		t.Fatal(err)
	}
	if stats.Retried != 1 || !fixture.pending || fixture.retryCount != 1 || fixture.lastError != "identity_not_available" {
		t.Fatalf("stats=%+v pending=%v retries=%d last_error=%q", stats, fixture.pending, fixture.retryCount, fixture.lastError)
	}
}

func TestResolveDueMovesUniqueIdentityToFormalArchive(t *testing.T) {
	store, fixture := newArchiveStoreTestFixture(archiveIdentityUnique, true)
	defer func() { _ = store.Close() }()
	stats, err := store.ResolveDue(context.Background(), fixture.item.CompletedAt, 1)
	if err != nil {
		t.Fatal(err)
	}
	if stats.Resolved != 1 || stats.Inserted != 1 || fixture.pending || !fixture.archived {
		t.Fatalf("stats=%+v pending=%v archived=%v", stats, fixture.pending, fixture.archived)
	}
}

func TestResolveDueDiscardsAmbiguousIdentityWithoutGuessing(t *testing.T) {
	store, fixture := newArchiveStoreTestFixture(archiveIdentityAmbiguous, true)
	defer func() { _ = store.Close() }()
	stats, err := store.ResolveDue(context.Background(), fixture.item.CompletedAt, 1)
	if err != nil {
		t.Fatal(err)
	}
	if stats.Ambiguous != 1 || fixture.pending || fixture.archived || fixture.rollbackCount != 1 {
		t.Fatalf("stats=%+v pending=%v archived=%v rollbacks=%d", stats, fixture.pending, fixture.archived, fixture.rollbackCount)
	}
}

func TestResolveDuePreservesPendingOnTechnicalResolverFailure(t *testing.T) {
	store, fixture := newArchiveStoreTestFixture(archiveIdentityFailure, true)
	defer func() { _ = store.Close() }()
	stats, err := store.ResolveDue(context.Background(), fixture.item.CompletedAt, 1)
	if err == nil {
		t.Fatal("expected resolver error")
	}
	if stats.Retried != 1 || !fixture.pending || fixture.retryCount != 1 || fixture.lastError == "" || fixture.rollbackCount != 1 {
		t.Fatalf("stats=%+v pending=%v retries=%d last_error=%q rollbacks=%d", stats, fixture.pending, fixture.retryCount, fixture.lastError, fixture.rollbackCount)
	}
}

func TestResolveDueDropsUnmatchedIdentityAfterDeadline(t *testing.T) {
	store, fixture := newArchiveStoreTestFixture(archiveIdentityMissing, true)
	defer func() { _ = store.Close() }()
	now := fixture.item.DeadlineAt
	stats, err := store.ResolveDue(context.Background(), now, 1)
	if err != nil {
		t.Fatal(err)
	}
	if stats.TimedOut != 1 || fixture.pending {
		t.Fatalf("stats=%+v pending=%v", stats, fixture.pending)
	}
}
