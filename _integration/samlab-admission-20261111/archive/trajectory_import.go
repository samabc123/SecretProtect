package main

import (
	"bufio"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/url"
	"os"
	"regexp"
	"strings"
	"time"
)

const (
	maxTrajectoryRecordBytes  = 16 << 20
	maxTrajectoryLineBytes    = maxTrajectoryRecordBytes + 64<<10
	maxTrajectoryToolBytes    = (1 << 20) + (8 << 10)
	maxTrajectoryMessageBytes = (2 << 20) + (8 << 10)
)

var windowsAbsolutePathPattern = regexp.MustCompile(`(?i)(^|[^A-Za-z0-9])((?:\\\\\?\\)?[a-z]:[\\/][^\s<>"']+)`)
var trajectoryURLPattern = regexp.MustCompile(`https?://[^\s<>"']+`)
var sha256Pattern = regexp.MustCompile(`^[0-9a-f]{64}$`)
var trajectorySourceIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)

var allowedTrajectoryEvents = map[string]struct{}{
	"user_message": {}, "assistant_message": {}, "tool_call": {}, "tool_result": {},
	"command_execution": {}, "file_change": {}, "web_search": {}, "collab_tool_call": {},
	"approval_request": {}, "approval_response": {}, "context_compacted": {},
	"media_operation": {}, "review_mode": {}, "turn_completed": {}, "turn_failed": {},
	"turn_aborted": {}, "turn_incomplete": {},
}

var trajectoryMediaKeys = map[string]struct{}{
	"data": {}, "b64_json": {}, "image_url": {}, "audio": {}, "local_audio": {},
	"local_images": {}, "images": {}, "base64": {}, "bytes": {},
}

func runTrajectoryCLI(ctx context.Context, cfg config, store archiveStore, args []string) (bool, error) {
	if len(args) == 0 {
		return false, nil
	}
	switch args[0] {
	case "resolve-user":
		flags := flag.NewFlagSet("resolve-user", flag.ContinueOnError)
		email := flags.String("email", "", "exact active Sub2API email")
		if err := flags.Parse(args[1:]); err != nil {
			return true, err
		}
		user, err := store.ResolveArchiveUserByEmail(ctx, *email)
		if err != nil {
			return true, err
		}
		return true, json.NewEncoder(os.Stdout).Encode(user)
	case "import-trajectories":
		flags := flag.NewFlagSet("import-trajectories", flag.ContinueOnError)
		email := flags.String("email", "", "exact active Sub2API email")
		path := flags.String("file", "", "gzip NDJSON archive package")
		if err := flags.Parse(args[1:]); err != nil {
			return true, err
		}
		if strings.TrimSpace(*path) == "" {
			return true, fmt.Errorf("--file is required")
		}
		result, err := importTrajectoryPackage(ctx, cfg, store, strings.TrimSpace(*email), *path)
		if encodeErr := json.NewEncoder(os.Stdout).Encode(result); encodeErr != nil && err == nil {
			err = encodeErr
		}
		return true, err
	default:
		return false, nil
	}
}

type trajectoryImportResult struct {
	BatchID   string                `json:"batch_id"`
	User      archiveUser           `json:"user"`
	Stats     trajectoryImportStats `json:"stats"`
	Duplicate bool                  `json:"duplicate_completed,omitempty"`
}

func importTrajectoryPackage(ctx context.Context, cfg config, store archiveStore, email, path string) (trajectoryImportResult, error) {
	user, err := store.ResolveArchiveUserByEmail(ctx, email)
	if err != nil {
		return trajectoryImportResult{}, err
	}
	manifestHash, err := sha256File(path)
	if err != nil {
		return trajectoryImportResult{}, err
	}
	file, err := os.Open(path)
	if err != nil {
		return trajectoryImportResult{}, err
	}
	defer func() { _ = file.Close() }()
	gzipReader, err := gzip.NewReader(file)
	if err != nil {
		return trajectoryImportResult{}, fmt.Errorf("open gzip package: %w", err)
	}
	defer func() { _ = gzipReader.Close() }()
	scanner := bufio.NewScanner(gzipReader)
	scanner.Buffer(make([]byte, 64*1024), maxTrajectoryLineBytes)
	if !scanner.Scan() {
		if err := scanner.Err(); err != nil {
			return trajectoryImportResult{}, err
		}
		return trajectoryImportResult{}, fmt.Errorf("trajectory package is empty")
	}
	var manifest trajectoryManifest
	if err := json.Unmarshal(scanner.Bytes(), &manifest); err != nil {
		return trajectoryImportResult{}, fmt.Errorf("decode manifest: %w", err)
	}
	if err := validateTrajectoryManifest(manifest); err != nil {
		return trajectoryImportResult{}, err
	}
	batch, err := store.StartTrajectoryBatch(ctx, trajectoryBatchStart{
		ID: manifest.BatchID, User: user, Source: manifest.Source,
		SchemaVersion: manifest.SchemaVersion, ManifestSHA256: manifestHash,
	})
	if err != nil {
		return trajectoryImportResult{}, err
	}
	result := trajectoryImportResult{BatchID: batch.ID, User: user, Duplicate: batch.AlreadyCompleted}
	if batch.AlreadyCompleted {
		return result, nil
	}
	privacy := newPrivacyClient(cfg)
	var errorTypes []string
	for scanner.Scan() {
		if len(strings.TrimSpace(scanner.Text())) == 0 {
			continue
		}
		result.Stats.Total++
		if len(scanner.Bytes()) > maxTrajectoryRecordBytes {
			result.Stats.Failed++
			errorTypes = append(errorTypes, "trajectory_too_large")
			continue
		}
		var record trajectoryImportRecord
		if err := json.Unmarshal(scanner.Bytes(), &record); err != nil {
			result.Stats.Failed++
			errorTypes = append(errorTypes, "invalid_json")
			continue
		}
		if err := canonicalizeTrajectoryTerminal(&record); err != nil {
			result.Stats.Failed++
			errorTypes = append(errorTypes, "validation_failed")
			continue
		}
		if err := validateTrajectoryRecord(record, time.Now().UTC()); err != nil {
			if errors.Is(err, errTrajectoryExpired) {
				result.Stats.Skipped++
				continue
			}
			result.Stats.Failed++
			errorTypes = append(errorTypes, "validation_failed")
			continue
		}
		redactCtx, cancel := context.WithTimeout(ctx, cfg.PrivacyTimeout)
		err := redactTrajectoryRecord(redactCtx, privacy, &record)
		cancel()
		if err != nil {
			result.Stats.Failed++
			errorTypes = append(errorTypes, "privacy_filter_failed")
			continue
		}
		action, err := store.ImportTrajectory(ctx, batch.ID, user.UserID, record)
		if err != nil {
			result.Stats.Failed++
			errorTypes = append(errorTypes, "database_write_failed")
			continue
		}
		switch action {
		case "inserted":
			result.Stats.Inserted++
		case "updated":
			result.Stats.Updated++
		default:
			result.Stats.Skipped++
		}
	}
	if err := scanner.Err(); err != nil {
		result.Stats.Failed++
		errorTypes = append(errorTypes, "package_read_failed")
	}
	errorSummary := strings.Join(uniqueStrings(errorTypes), ",")
	if err := store.FinishTrajectoryBatch(ctx, batch.ID, result.Stats, errorSummary); err != nil {
		return result, err
	}
	if result.Stats.Failed > 0 {
		return result, fmt.Errorf("trajectory import completed with %d failed records", result.Stats.Failed)
	}
	return result, nil
}

var errTrajectoryExpired = errors.New("trajectory is already expired")

func canonicalizeTrajectoryTerminal(record *trajectoryImportRecord) error {
	expectedTerminal := "turn_" + record.Status
	terminalIndex := -1
	for index := range record.Messages {
		// Public clients released before terminalfix included parser-only
		// deduplication metadata. Accept it for wire compatibility, but never
		// persist it as part of the archived logical trajectory.
		record.Messages[index].Extra.LegacySource = ""
		record.Messages[index].Extra.LegacyKind = ""
		eventType := strings.ToLower(record.Messages[index].Extra.EventType)
		if eventType != "turn_completed" && eventType != "turn_failed" &&
			eventType != "turn_aborted" && eventType != "turn_incomplete" {
			continue
		}
		if eventType != expectedTerminal || record.Messages[index].Extra.Status != record.Status {
			return fmt.Errorf("terminal event does not match trajectory status")
		}
		terminalIndex = index
	}
	if terminalIndex < 0 {
		return fmt.Errorf("trajectory terminal event is missing")
	}
	terminal := record.Messages[terminalIndex]
	canonical := make([]trajectoryMessage, 0, len(record.Messages))
	for index := range record.Messages {
		eventType := strings.ToLower(record.Messages[index].Extra.EventType)
		if eventType == "turn_completed" || eventType == "turn_failed" ||
			eventType == "turn_aborted" || eventType == "turn_incomplete" {
			continue
		}
		canonical = append(canonical, record.Messages[index])
	}
	canonical = append(canonical, terminal)
	for index := range canonical {
		canonical[index].Sequence = index + 1
	}
	record.Messages = canonical
	record.Metrics.EventCount = len(canonical)
	return nil
}

func validateTrajectoryManifest(manifest trajectoryManifest) error {
	if manifest.RecordType != "manifest" || manifest.SchemaVersion != trajectorySchemaVersion {
		return fmt.Errorf("unsupported trajectory manifest")
	}
	if !validUUID(manifest.BatchID) {
		return fmt.Errorf("manifest batch_id is not a UUID")
	}
	if manifest.Source != "rollout" && manifest.Source != "app-server" {
		return fmt.Errorf("unsupported manifest source")
	}
	if manifest.Profile != "atif" || manifest.RetentionDays != 30 {
		return fmt.Errorf("manifest must be canonical atif with 30 day retention")
	}
	return nil
}

func validateTrajectoryRecord(record trajectoryImportRecord, now time.Time) error {
	if record.RecordType != "trajectory" || record.SchemaVersion != trajectorySchemaVersion || record.Source != "codex_local" {
		return fmt.Errorf("unsupported trajectory record")
	}
	if !trajectorySourceIDPattern.MatchString(record.SourceThreadID) || !trajectorySourceIDPattern.MatchString(record.SourceTurnID) {
		return fmt.Errorf("invalid source identifiers")
	}
	if record.TrajectoryID != "codex:"+record.SourceThreadID+":"+record.SourceTurnID || len(record.TrajectoryID) > 384 {
		return fmt.Errorf("trajectory_id does not match source identifiers")
	}
	if len(record.Title) > maxTrajectoryMessageBytes || len(record.FinalText) > maxTrajectoryMessageBytes ||
		len(record.Model) > 255 || len(record.ModelProvider) > 128 {
		return fmt.Errorf("trajectory metadata exceeds size limit")
	}
	if record.Status != "completed" && record.Status != "failed" && record.Status != "aborted" && record.Status != "incomplete" {
		return fmt.Errorf("invalid trajectory status")
	}
	startedAt, err := time.Parse(time.RFC3339, record.StartedAt)
	if err != nil {
		return fmt.Errorf("invalid started_at")
	}
	completedAt, err := time.Parse(time.RFC3339, record.CompletedAt)
	if err != nil || completedAt.Before(startedAt) {
		return fmt.Errorf("invalid completed_at")
	}
	if completedAt.After(now.Add(5*time.Minute)) || startedAt.After(now.Add(5*time.Minute)) {
		return fmt.Errorf("trajectory timestamps are in the future")
	}
	if !completedAt.Add(30 * 24 * time.Hour).After(now) {
		return errTrajectoryExpired
	}
	expiresAt, err := time.Parse(time.RFC3339, record.ExpiresAt)
	if err != nil || expiresAt.Sub(completedAt.Add(30*24*time.Hour)) > time.Second ||
		completedAt.Add(30*24*time.Hour).Sub(expiresAt) > time.Second {
		return fmt.Errorf("invalid expires_at")
	}
	if record.Status == "incomplete" && completedAt.After(now.Add(-24*time.Hour)) {
		return fmt.Errorf("incomplete trajectory is still active")
	}
	if record.DurationMS < 0 || len(record.Messages) == 0 || len(record.Messages) > 10000 {
		return fmt.Errorf("invalid trajectory metrics")
	}
	if record.Metrics.EventCount != len(record.Messages) || record.Metrics.ToolCallCount < 0 || record.Metrics.PrivacyEntities < 0 {
		return fmt.Errorf("trajectory metrics do not match messages")
	}
	if !sha256Pattern.MatchString(record.ContentSHA256) {
		return fmt.Errorf("invalid trajectory content hash")
	}
	toolCalls := 0
	for index, message := range record.Messages {
		if message.Sequence != index+1 {
			return fmt.Errorf("message sequence is not contiguous")
		}
		if message.Role != "user" && message.Role != "assistant" && message.Role != "tool" {
			return fmt.Errorf("invalid message role")
		}
		eventType := strings.ToLower(message.Extra.EventType)
		if _, ok := allowedTrajectoryEvents[eventType]; !ok {
			return fmt.Errorf("forbidden event type")
		}
		if !trajectoryEventRoleAllowed(eventType, message.Role) {
			return fmt.Errorf("event role does not match event type")
		}
		if message.Extra.Phase != "" && message.Extra.Phase != "commentary" &&
			message.Extra.Phase != "final_answer" && message.Extra.Phase != "legacy_unknown" {
			return fmt.Errorf("forbidden assistant phase")
		}
		if message.Extra.Phase != "" && eventType != "assistant_message" {
			return fmt.Errorf("phase is only valid for assistant messages")
		}
		if _, err := time.Parse(time.RFC3339, message.Timestamp); err != nil {
			return fmt.Errorf("invalid message timestamp")
		}
		var content any
		if len(message.Content) == 0 || json.Unmarshal(message.Content, &content) != nil {
			return fmt.Errorf("message content is not valid JSON")
		}
		limit := maxTrajectoryMessageBytes
		if trajectoryToolEvent(eventType) || message.Role == "tool" {
			limit = maxTrajectoryToolBytes
		}
		if len(message.Content)+len(message.ToolCalls) > limit {
			return fmt.Errorf("message exceeds import size limit")
		}
		if len(message.ToolCalls) > 0 {
			var tools any
			if eventType != "tool_call" || json.Unmarshal(message.ToolCalls, &tools) != nil {
				return fmt.Errorf("invalid tool_calls payload")
			}
			toolCalls++
		}
		if message.Extra.OriginalBytes < message.Extra.StoredBytes || message.Extra.StoredBytes < 0 {
			return fmt.Errorf("invalid truncation metadata")
		}
		if len(message.Extra.SourceItemID) > 255 || len(message.Extra.ToolCallID) > 255 ||
			len(message.Extra.ToolName) > 255 || len(message.Extra.Status) > 64 || len(message.Name) > 255 ||
			len(message.ToolCallID) > 255 || !sha256Pattern.MatchString(message.Extra.ContentSHA256) {
			return fmt.Errorf("invalid event metadata")
		}
	}
	if toolCalls != record.Metrics.ToolCallCount {
		return fmt.Errorf("tool call metric does not match messages")
	}
	expectedTerminal := "turn_" + record.Status
	last := record.Messages[len(record.Messages)-1]
	if last.Extra.EventType != expectedTerminal || last.Extra.Status != record.Status {
		return fmt.Errorf("trajectory terminal event does not match status")
	}
	return nil
}

func trajectoryToolEvent(eventType string) bool {
	switch eventType {
	case "tool_call", "tool_result", "command_execution", "file_change", "web_search", "collab_tool_call", "media_operation":
		return true
	default:
		return false
	}
}

func trajectoryEventRoleAllowed(eventType, role string) bool {
	switch eventType {
	case "user_message", "approval_response":
		return role == "user" || (eventType == "approval_response" && role == "tool")
	case "assistant_message", "tool_call", "web_search", "collab_tool_call", "review_mode", "approval_request":
		return role == "assistant"
	case "tool_result", "command_execution", "file_change", "media_operation", "context_compacted",
		"turn_completed", "turn_failed", "turn_aborted", "turn_incomplete":
		return role == "tool"
	default:
		return false
	}
}

func redactTrajectoryRecord(ctx context.Context, privacy *privacyClient, record *trajectoryImportRecord) error {
	var count int
	redactText := func(value string) (string, error) {
		clean := sanitizeTrajectoryText(value)
		results, err := privacy.redactBatch(ctx, []string{clean})
		if err != nil {
			return "", err
		}
		if len(results) != 1 {
			return "", fmt.Errorf("privacy filter returned invalid result count")
		}
		count += results[0].Count
		return results[0].Redacted, nil
	}
	var err error
	if record.Title, err = redactText(record.Title); err != nil {
		return err
	}
	for index := range record.Messages {
		message := &record.Messages[index]
		var contentValue any
		if err := json.Unmarshal(message.Content, &contentValue); err != nil {
			return err
		}
		document := map[string]any{"content": contentValue}
		if len(message.ToolCalls) > 0 {
			var tools any
			if err := json.Unmarshal(message.ToolCalls, &tools); err != nil {
				return err
			}
			document["tool_calls"] = tools
		}
		eventCount, err := redactTrajectoryDocument(ctx, privacy, document)
		if err != nil {
			return err
		}
		count += eventCount
		contentBytes, err := json.Marshal(document["content"])
		if err != nil {
			return err
		}
		message.Content = contentBytes
		if tools, ok := document["tool_calls"]; ok {
			message.ToolCalls, err = json.Marshal(tools)
			if err != nil {
				return err
			}
		}
		message.Extra.StoredBytes = int64(len(message.Content) + len(message.ToolCalls))
		if !message.Extra.Truncated || message.Extra.OriginalBytes < message.Extra.StoredBytes {
			message.Extra.OriginalBytes = message.Extra.StoredBytes
		}
		message.Extra.PrivacyEntities = eventCount
		composite, _ := json.Marshal(document)
		digest := sha256.Sum256(composite)
		message.Extra.ContentSHA256 = hex.EncodeToString(digest[:])
	}
	record.FinalText = finalTextFromTrajectoryMessages(record.Messages)
	record.PrivacyEntities = count
	record.Metrics.PrivacyEntities = count
	record.Metrics.EventCount = len(record.Messages)
	record.Metrics.ToolCallCount = 0
	for _, message := range record.Messages {
		if message.Extra.EventType == "tool_call" {
			record.Metrics.ToolCallCount++
		}
	}
	return rehashTrajectoryRecord(record)
}

func redactTrajectoryRecordTwice(ctx context.Context, privacy *privacyClient, record *trajectoryImportRecord) error {
	if err := redactTrajectoryRecord(ctx, privacy, record); err != nil {
		return err
	}
	firstPassEntities := record.PrivacyEntities
	firstPassMessageEntities := make([]int, len(record.Messages))
	for index := range record.Messages {
		firstPassMessageEntities[index] = record.Messages[index].Extra.PrivacyEntities
	}
	if err := redactTrajectoryRecord(ctx, privacy, record); err != nil {
		return err
	}
	for index := range record.Messages {
		record.Messages[index].Extra.PrivacyEntities += firstPassMessageEntities[index]
	}
	record.PrivacyEntities += firstPassEntities
	record.Metrics.PrivacyEntities = record.PrivacyEntities
	return rehashTrajectoryRecord(record)
}

func rehashTrajectoryRecord(record *trajectoryImportRecord) error {
	copyRecord := *record
	copyRecord.ContentSHA256 = ""
	payload, err := json.Marshal(copyRecord)
	if err != nil {
		return err
	}
	digest := sha256.Sum256(payload)
	record.ContentSHA256 = hex.EncodeToString(digest[:])
	return nil
}

type trajectoryRedactionTarget struct {
	value string
	set   func(string)
}

func redactTrajectoryDocument(ctx context.Context, privacy *privacyClient, document any) (int, error) {
	sanitizePostgresJSONStrings(document)
	sanitizeTrajectoryDocumentKeys(document)
	targets := make([]trajectoryRedactionTarget, 0, 16)
	var walk func(any, bool)
	walk = func(node any, active bool) {
		switch value := node.(type) {
		case map[string]any:
			for rawKey, child := range value {
				key := rawKey
				lower := strings.ToLower(key)
				if _, isMedia := trajectoryMediaKeys[lower]; isMedia {
					value[key] = "[media omitted]"
					continue
				}
				if lower == "id" || lower == "type" || lower == "role" || lower == "status" || lower == "tool_call_id" || lower == "name" {
					continue
				}
				childActive := active || trajectoryPrivacyTextKey(lower)
				if text, ok := child.(string); ok && childActive {
					targets = append(targets, trajectoryRedactionTarget{value: sanitizeTrajectoryText(text), set: func(redacted string) { value[key] = redacted }})
					continue
				}
				walk(child, childActive)
			}
		case []any:
			for index := range value {
				itemIndex := index
				if text, ok := value[itemIndex].(string); ok && active {
					targets = append(targets, trajectoryRedactionTarget{value: sanitizeTrajectoryText(text), set: func(redacted string) { value[itemIndex] = redacted }})
					continue
				}
				walk(value[itemIndex], active)
			}
		}
	}
	walk(document, false)
	entities := 0
	for start := 0; start < len(targets); start += 64 {
		end := start + 64
		if end > len(targets) {
			end = len(targets)
		}
		texts := make([]string, end-start)
		for index := start; index < end; index++ {
			texts[index-start] = targets[index].value
		}
		results, err := privacy.redactBatch(ctx, texts)
		if err != nil {
			return 0, err
		}
		if len(results) != len(texts) {
			return 0, fmt.Errorf("privacy filter returned invalid result count")
		}
		for index, result := range results {
			targets[start+index].set(result.Redacted)
			entities += result.Count
		}
	}
	sanitizePostgresJSONStrings(document)
	return entities, nil
}

func sanitizeTrajectoryDocumentKeys(document any) {
	switch value := document.(type) {
	case map[string]any:
		keys := make([]string, 0, len(value))
		for key := range value {
			keys = append(keys, key)
		}
		for _, key := range keys {
			child := value[key]
			cleanKey := sanitizeTrajectoryText(key)
			if cleanKey != key {
				delete(value, key)
				if _, exists := value[cleanKey]; exists {
					digest := sha256.Sum256([]byte(key))
					cleanKey += "#" + hex.EncodeToString(digest[:4])
				}
				value[cleanKey] = child
			}
			sanitizeTrajectoryDocumentKeys(child)
		}
	case []any:
		for _, child := range value {
			sanitizeTrajectoryDocumentKeys(child)
		}
	}
}

func finalTextFromTrajectoryMessages(messages []trajectoryMessage) string {
	for index := len(messages) - 1; index >= 0; index-- {
		message := messages[index]
		if message.Role != "assistant" || (message.Extra.Phase != "final_answer" && !message.Extra.IsFinal) {
			continue
		}
		var content any
		if err := json.Unmarshal(message.Content, &content); err != nil {
			return ""
		}
		switch value := content.(type) {
		case string:
			return value
		case []any:
			var builder strings.Builder
			for _, item := range value {
				switch part := item.(type) {
				case string:
					builder.WriteString(part)
				case map[string]any:
					if text, ok := part["text"].(string); ok {
						builder.WriteString(text)
					}
				}
			}
			return builder.String()
		case map[string]any:
			if text, ok := value["text"].(string); ok {
				return text
			}
			if preview, ok := value["preview"].(string); ok && value["_archive_truncated"] == true {
				return preview
			}
		}
	}
	return ""
}

func trajectoryPrivacyTextKey(key string) bool {
	switch key {
	case "content", "text", "message", "prompt", "arguments", "input", "output",
		"result", "error", "command", "cwd", "diff", "path", "query", "review",
		"additionaldetails", "structuredcontent", "tool_calls":
		return true
	default:
		return false
	}
}

func sanitizeTrajectoryText(value string) string {
	value = sanitizePostgresText(value)
	value = strings.ReplaceAll(value, `\\?\`, "")
	value = windowsAbsolutePathPattern.ReplaceAllString(value, `${1}<ABSOLUTE_PATH>`)
	value = trajectoryURLPattern.ReplaceAllStringFunc(value, func(raw string) string {
		trailing := ""
		for len(raw) > 0 && strings.ContainsRune(".,);]}:", rune(raw[len(raw)-1])) {
			trailing = raw[len(raw)-1:] + trailing
			raw = raw[:len(raw)-1]
		}
		parsed, err := url.Parse(raw)
		if err != nil {
			return "<URL>" + trailing
		}
		parsed.RawQuery = ""
		parsed.Fragment = ""
		return parsed.String() + trailing
	})
	return value
}

func sha256File(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer func() { _ = file.Close() }()
	digest := sha256.New()
	if _, err := io.Copy(digest, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(digest.Sum(nil)), nil
}

func uniqueStrings(values []string) []string {
	seen := map[string]struct{}{}
	output := make([]string, 0, len(values))
	for _, value := range values {
		if _, ok := seen[value]; ok {
			continue
		}
		seen[value] = struct{}{}
		output = append(output, value)
	}
	return output
}
