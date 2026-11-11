package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"strings"
)

const (
	omittedImage = "[image omitted]"
	omittedAudio = "[audio omitted]"
	omittedFile  = "[file omitted]"
)

// compactArchiveRequest builds the payload that is allowed to cross the
// archive boundary. The original request is still forwarded upstream, but it
// must never be retained here: only the last user message and minimal routing
// metadata are kept.
func compactArchiveRequest(raw []byte, protocol streamProtocol) (json.RawMessage, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var source map[string]any
	if err := decoder.Decode(&source); err != nil {
		return nil, fmt.Errorf("decode archive request: %w", err)
	}
	if decoder.Decode(&struct{}{}) != io.EOF {
		return nil, fmt.Errorf("decode archive request: trailing JSON value")
	}

	compact := make(map[string]any, 4)
	if model, ok := source["model"].(string); ok && model != "" {
		compact["model"] = model
	}
	if stream, ok := source["stream"].(bool); ok {
		compact["stream"] = stream
	}

	omitted := 0
	found := false
	switch protocol {
	case protocolOpenAIChat, protocolAnthropic:
		if message, ok := lastUserMessage(source["messages"], false); ok {
			compact["messages"] = []any{compactMessage(message, "content", &omitted)}
			found = true
		} else {
			compact["messages"] = []any{}
		}
	case protocolOpenAIResponses:
		switch input := source["input"].(type) {
		case string:
			compact["input"] = input
			found = true
		case []any:
			if message, ok := lastUserMessage(input, false); ok {
				compact["input"] = []any{compactMessage(message, "content", &omitted)}
				found = true
			} else if contentBlocksOnly(input) {
				compact["input"] = []any{map[string]any{
					"role": "user", "content": compactArchiveContent(input, &omitted),
				}}
				found = len(input) > 0
			} else {
				compact["input"] = []any{}
			}
		default:
			compact["input"] = []any{}
		}
	case protocolGemini:
		if message, ok := lastUserMessage(source["contents"], true); ok {
			compact["contents"] = []any{compactMessage(message, "parts", &omitted)}
			found = true
		} else {
			compact["contents"] = []any{}
		}
	default:
		return nil, fmt.Errorf("unsupported archive protocol %s", protocol.String())
	}

	compact["_archive"] = map[string]any{
		"mode":               "last_user_message",
		"user_message_found": found,
		"media_omitted":      omitted,
	}
	encoded, err := json.Marshal(compact)
	if err != nil {
		return nil, fmt.Errorf("encode compact archive request: %w", err)
	}
	return encoded, nil
}

func emptyCompactArchiveRequest(protocol streamProtocol) json.RawMessage {
	key := "input"
	switch protocol {
	case protocolOpenAIChat, protocolAnthropic:
		key = "messages"
	case protocolGemini:
		key = "contents"
	}
	encoded, _ := json.Marshal(map[string]any{
		key: []any{},
		"_archive": map[string]any{
			"mode": "last_user_message", "user_message_found": false,
			"media_omitted": 0, "compaction_error": true,
		},
	})
	return encoded
}

func lastUserMessage(value any, allowMissingRole bool) (map[string]any, bool) {
	items, ok := value.([]any)
	if !ok {
		return nil, false
	}
	for index := len(items) - 1; index >= 0; index-- {
		message, ok := items[index].(map[string]any)
		if ok && strings.EqualFold(strings.TrimSpace(stringValue(message["role"], "")), "user") {
			return message, true
		}
	}
	if allowMissingRole {
		for index := len(items) - 1; index >= 0; index-- {
			message, ok := items[index].(map[string]any)
			if ok && strings.TrimSpace(stringValue(message["role"], "")) == "" {
				return message, true
			}
		}
	}
	return nil, false
}

func compactMessage(message map[string]any, contentKey string, omitted *int) map[string]any {
	result := map[string]any{"role": "user"}
	content, exists := message[contentKey]
	if !exists {
		content = ""
	}
	result[contentKey] = compactArchiveContent(content, omitted)
	return result
}

func contentBlocksOnly(items []any) bool {
	if len(items) == 0 {
		return false
	}
	for _, item := range items {
		block, ok := item.(map[string]any)
		if !ok {
			return false
		}
		if strings.TrimSpace(stringValue(block["role"], "")) != "" {
			return false
		}
	}
	return true
}

func compactArchiveContent(value any, omitted *int) any {
	switch content := value.(type) {
	case string:
		if kind := embeddedMediaKind(content); kind != "" {
			*omitted++
			return mediaPlaceholder(kind)
		}
		return content
	case []any:
		result := make([]any, 0, len(content))
		for _, item := range content {
			result = append(result, compactArchiveContent(item, omitted))
		}
		return result
	case map[string]any:
		if kind := archiveMediaKind(content); kind != "" {
			*omitted++
			return map[string]any{
				"type": kind,
				"text": mediaPlaceholder(kind),
			}
		}
		result := make(map[string]any, 4)
		for _, key := range []string{"type", "text", "input_text", "value", "content", "parts"} {
			child, exists := content[key]
			if !exists {
				continue
			}
			if key == "type" {
				if text, ok := child.(string); ok {
					result[key] = text
				}
				continue
			}
			result[key] = compactArchiveContent(child, omitted)
		}
		return result
	default:
		return nil
	}
}

func archiveMediaKind(block map[string]any) string {
	typeName := strings.ToLower(strings.TrimSpace(stringValue(block["type"], "")))
	switch {
	case strings.Contains(typeName, "image"):
		return "image"
	case strings.Contains(typeName, "audio"):
		return "audio"
	case strings.Contains(typeName, "file"), strings.Contains(typeName, "document"), strings.Contains(typeName, "pdf"):
		return "file"
	}
	for _, key := range []string{"image_url", "b64_json", "local_images", "images"} {
		if _, exists := block[key]; exists {
			return "image"
		}
	}
	for _, key := range []string{"audio", "local_audio"} {
		if _, exists := block[key]; exists {
			return "audio"
		}
	}
	for _, key := range []string{"file_data", "file_url", "file_id"} {
		if _, exists := block[key]; exists {
			return "file"
		}
	}
	for _, key := range []string{"source", "inlineData", "inline_data", "fileData", "file_data"} {
		if nested, ok := block[key].(map[string]any); ok {
			mime := strings.ToLower(firstNonEmpty(
				stringValue(nested["media_type"], ""), stringValue(nested["mime_type"], ""),
				stringValue(nested["mimeType"], ""),
			))
			switch {
			case strings.HasPrefix(mime, "image/"):
				return "image"
			case strings.HasPrefix(mime, "audio/"):
				return "audio"
			case mime != "":
				return "file"
			}
			if _, hasData := nested["data"]; hasData {
				return "file"
			}
		}
	}
	return ""
}

func embeddedMediaKind(value string) string {
	lower := strings.ToLower(strings.TrimSpace(value))
	switch {
	case strings.HasPrefix(lower, "data:image/"):
		return "image"
	case strings.HasPrefix(lower, "data:audio/"):
		return "audio"
	case strings.HasPrefix(lower, "data:application/"), strings.HasPrefix(lower, "data:video/"):
		return "file"
	default:
		return ""
	}
}

func mediaPlaceholder(kind string) string {
	switch kind {
	case "image":
		return omittedImage
	case "audio":
		return omittedAudio
	default:
		return omittedFile
	}
}
