package main

import (
	"compress/gzip"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func validTrajectoryRecord(now time.Time) trajectoryImportRecord {
	started := now.Add(-time.Minute).UTC().Truncate(time.Second)
	completed := now.UTC().Truncate(time.Second)
	hash := strings.Repeat("a", 64)
	messages := []trajectoryMessage{
		{
			Sequence: 1, Role: "user", Content: json.RawMessage(`{"text":"hello"}`), Timestamp: started.Format(time.RFC3339),
			Extra: trajectoryMessageExtra{EventType: "user_message", OriginalBytes: 16, StoredBytes: 16, ContentSHA256: hash},
		},
		{
			Sequence: 2, Role: "assistant", Content: json.RawMessage(`"complete answer"`), Timestamp: completed.Format(time.RFC3339),
			Extra: trajectoryMessageExtra{EventType: "assistant_message", Phase: "final_answer", IsFinal: true, OriginalBytes: 17, StoredBytes: 17, ContentSHA256: hash},
		},
		{
			Sequence: 3, Role: "tool", Content: json.RawMessage(`{}`), Timestamp: completed.Format(time.RFC3339),
			Extra: trajectoryMessageExtra{EventType: "turn_completed", Status: "completed", OriginalBytes: 2, StoredBytes: 2, ContentSHA256: hash},
		},
	}
	return trajectoryImportRecord{
		RecordType: "trajectory", SchemaVersion: trajectorySchemaVersion,
		TrajectoryID: "codex:thread-1:turn-1", Source: "codex_local",
		SourceThreadID: "thread-1", SourceTurnID: "turn-1",
		Title: "test", Model: "gpt-test", ModelProvider: "openai", Status: "completed",
		StartedAt: started.Format(time.RFC3339), CompletedAt: completed.Format(time.RFC3339),
		DurationMS: 60000, ExpiresAt: completed.Add(30 * 24 * time.Hour).Format(time.RFC3339),
		Messages: messages, FinalText: "complete answer",
		Metrics: trajectoryMetrics{EventCount: len(messages)}, ContentSHA256: hash,
	}
}

func TestValidateTrajectoryRecordRejectsHiddenOrInconsistentEvents(t *testing.T) {
	now := time.Date(2026, time.August, 6, 6, 0, 0, 0, time.UTC)
	valid := validTrajectoryRecord(now)
	if err := validateTrajectoryRecord(valid, now); err != nil {
		t.Fatalf("valid record rejected: %v", err)
	}

	for name, mutate := range map[string]func(*trajectoryImportRecord){
		"reasoning phase":   func(record *trajectoryImportRecord) { record.Messages[1].Extra.Phase = "reasoning" },
		"delta event":       func(record *trajectoryImportRecord) { record.Messages[1].Extra.EventType = "response_delta" },
		"terminal mismatch": func(record *trajectoryImportRecord) { record.Messages[2].Extra.EventType = "turn_failed" },
		"forged metrics":    func(record *trajectoryImportRecord) { record.Metrics.EventCount++ },
	} {
		t.Run(name, func(t *testing.T) {
			record := validTrajectoryRecord(now)
			mutate(&record)
			if err := validateTrajectoryRecord(record, now); err == nil {
				t.Fatal("expected validation failure")
			}
		})
	}
}

func TestValidateTrajectoryRecordRejectsActiveIncompleteAndExpired(t *testing.T) {
	now := time.Date(2026, time.August, 6, 6, 0, 0, 0, time.UTC)
	active := validTrajectoryRecord(now)
	active.Status = "incomplete"
	active.Messages[2].Extra.EventType = "turn_incomplete"
	active.Messages[2].Extra.Status = "incomplete"
	if err := validateTrajectoryRecord(active, now); err == nil {
		t.Fatal("active incomplete trajectory was accepted")
	}

	expired := validTrajectoryRecord(now.Add(-31 * 24 * time.Hour))
	if err := validateTrajectoryRecord(expired, now); err != errTrajectoryExpired {
		t.Fatalf("expired error=%v, want %v", err, errTrajectoryExpired)
	}
}

func TestRedactTrajectoryRecordRemovesMediaPathsQueriesAndRebuildsFinalText(t *testing.T) {
	privacy := newPrivacyTestServer(t, func(text string) (string, bool, int) {
		redacted := strings.ReplaceAll(text, "alice@example.com", "[EMAIL]")
		redacted = strings.ReplaceAll(redacted, "SuperSecret123", "[SECRET]")
		return redacted, redacted != text, 1
	})
	defer privacy.Close()
	privacyURL, _ := url.Parse(privacy.URL)
	record := validTrajectoryRecord(time.Now().UTC())
	record.Title = "alice@example.com"
	record.FinalText = "client supplied value must not win"
	record.Messages[0].Content = json.RawMessage(`{"text":"alice@example.com SuperSecret123 C:\\Users\\alice\\repo\\x https://example.test/path?token=SuperSecret123","data":"QUJDREVGRw=="}`)
	record.Messages[1].Content = json.RawMessage(`"answer for alice@example.com"`)
	client := newPrivacyClient(config{PrivacyFilterURL: privacyURL, PrivacyTimeout: 2 * time.Second, PrivacyMaxBody: 16 << 20})
	if err := redactTrajectoryRecord(context.Background(), client, &record); err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(record)
	text := string(raw)
	for _, forbidden := range []string{"alice@example.com", "SuperSecret123", `C:\\Users\\alice`, "token=", "QUJDREVGRw==", "client supplied value"} {
		if strings.Contains(text, forbidden) {
			t.Fatalf("redacted record still contains %q: %s", forbidden, text)
		}
	}
	if record.FinalText != "answer for [EMAIL]" {
		t.Fatalf("final_text=%q", record.FinalText)
	}
	var firstContent map[string]any
	if err := json.Unmarshal(record.Messages[0].Content, &firstContent); err != nil {
		t.Fatal(err)
	}
	contentText, _ := firstContent["text"].(string)
	if firstContent["data"] != "[media omitted]" || !strings.Contains(contentText, "<ABSOLUTE_PATH>") ||
		!strings.Contains(contentText, "https://example.test/path") {
		t.Fatalf("media/path placeholders missing: %s", text)
	}
}

func TestImportTrajectoryPackageFailsClosedWhenPrivacyFilterFails(t *testing.T) {
	privacy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "unavailable", http.StatusBadGateway)
	}))
	defer privacy.Close()
	privacyURL, _ := url.Parse(privacy.URL)
	record := validTrajectoryRecord(time.Now().UTC())
	path := writeTrajectoryPackage(t, record)
	store := &memoryArchiveStore{}
	result, err := importTrajectoryPackage(context.Background(), config{
		PrivacyFilterURL: privacyURL, PrivacyTimeout: 2 * time.Second, PrivacyMaxBody: 16 << 20,
	}, store, "tester@example.com", path)
	if err == nil {
		t.Fatal("expected fail-closed import error")
	}
	if result.Stats.Failed != 1 || len(store.trajectoryImports) != 0 || store.trajectoryError != "privacy_filter_failed" {
		t.Fatalf("unexpected import result=%+v imports=%d error=%q", result, len(store.trajectoryImports), store.trajectoryError)
	}
}

func writeTrajectoryPackage(t *testing.T, record trajectoryImportRecord) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "trajectory.ndjson.gz")
	file, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	writer := gzip.NewWriter(file)
	encoder := json.NewEncoder(writer)
	manifest := trajectoryManifest{
		RecordType: "manifest", SchemaVersion: trajectorySchemaVersion,
		BatchID: "9f7307de-ea68-41ac-bdea-059098ae1a82", ExportedAt: time.Now().UTC().Format(time.RFC3339),
		Source: "rollout", Profile: "atif", RetentionDays: 30,
	}
	if err := encoder.Encode(manifest); err != nil {
		t.Fatal(err)
	}
	if err := encoder.Encode(record); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
	return path
}
