package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

type streamProtocol int

const (
	protocolUnknown streamProtocol = iota
	protocolOpenAIChat
	protocolOpenAIResponses
	protocolAnthropic
	protocolGemini
)

func (p streamProtocol) String() string {
	switch p {
	case protocolOpenAIChat:
		return "openai_chat_completions"
	case protocolOpenAIResponses:
		return "openai_responses"
	case protocolAnthropic:
		return "anthropic_messages"
	case protocolGemini:
		return "gemini_stream_generate_content"
	default:
		return "unknown"
	}
}

func protocolForPath(path string) streamProtocol {
	switch path {
	case "/v1/chat/completions", "/chat/completions":
		return protocolOpenAIChat
	case "/v1/messages", "/antigravity/v1/messages":
		return protocolAnthropic
	}
	if isResponsesPath(path) {
		return protocolOpenAIResponses
	}
	if strings.Contains(path, ":streamGenerateContent") {
		return protocolGemini
	}
	return protocolUnknown
}

type sseEvent struct {
	Name string
	Data []byte
}

type sseParser struct {
	pending   []byte
	eventName string
	dataLines [][]byte
	consume   func(sseEvent)
}

func newSSEParser(consume func(sseEvent)) *sseParser {
	return &sseParser{consume: consume}
}

func (p *sseParser) Write(chunk []byte) {
	p.pending = append(p.pending, chunk...)
	for {
		index := bytes.IndexByte(p.pending, '\n')
		if index < 0 {
			return
		}
		line := append([]byte(nil), p.pending[:index]...)
		p.pending = p.pending[index+1:]
		p.processLine(bytes.TrimSuffix(line, []byte{'\r'}))
	}
}

func (p *sseParser) Finish() {
	if len(p.pending) > 0 {
		p.processLine(bytes.TrimSuffix(p.pending, []byte{'\r'}))
		p.pending = nil
	}
	p.emit()
}

func (p *sseParser) processLine(line []byte) {
	if len(line) == 0 {
		p.emit()
		return
	}
	if line[0] == ':' {
		return
	}
	field, value, found := bytes.Cut(line, []byte{':'})
	if !found {
		value = nil
	}
	value = bytes.TrimPrefix(value, []byte{' '})
	switch string(field) {
	case "event":
		p.eventName = string(value)
	case "data":
		p.dataLines = append(p.dataLines, append([]byte(nil), value...))
	}
}

func (p *sseParser) emit() {
	if len(p.dataLines) == 0 && p.eventName == "" {
		return
	}
	data := bytes.Join(p.dataLines, []byte{'\n'})
	p.consume(sseEvent{Name: p.eventName, Data: data})
	p.eventName = ""
	p.dataLines = nil
}

type archivedToolCall struct {
	ID        string `json:"id,omitempty"`
	Type      string `json:"type"`
	Name      string `json:"name,omitempty"`
	Arguments any    `json:"arguments,omitempty"`
}

type toolCallBuilder struct {
	ID        string
	Type      string
	Name      string
	Arguments strings.Builder
	Input     any
}

type conversationAccumulator struct {
	protocol     streamProtocol
	text         strings.Builder
	tools        map[string]*toolCallBuilder
	toolOrder    []string
	finishReason string
	complete     bool
}

func newConversationAccumulator(protocol streamProtocol) *conversationAccumulator {
	return &conversationAccumulator{protocol: protocol, tools: make(map[string]*toolCallBuilder)}
}

func (a *conversationAccumulator) consume(event sseEvent) {
	switch a.protocol {
	case protocolOpenAIChat:
		a.consumeOpenAIChat(event)
	case protocolOpenAIResponses:
		a.consumeOpenAIResponses(event)
	case protocolAnthropic:
		a.consumeAnthropic(event)
	case protocolGemini:
		a.consumeGemini(event)
	}
}

func (a *conversationAccumulator) tool(key string) *toolCallBuilder {
	if key == "" {
		key = strconv.Itoa(len(a.toolOrder))
	}
	if tool := a.tools[key]; tool != nil {
		return tool
	}
	tool := &toolCallBuilder{Type: "function"}
	a.tools[key] = tool
	a.toolOrder = append(a.toolOrder, key)
	return tool
}

func decodeObject(data []byte) map[string]any {
	var value map[string]any
	if json.Unmarshal(data, &value) != nil {
		return nil
	}
	return value
}

func (a *conversationAccumulator) consumeOpenAIChat(event sseEvent) {
	if string(bytes.TrimSpace(event.Data)) == "[DONE]" {
		a.complete = true
		return
	}
	body := decodeObject(event.Data)
	for _, rawChoice := range anySlice(body["choices"]) {
		choice := anyMap(rawChoice)
		delta := anyMap(choice["delta"])
		a.appendVisibleContent(delta["content"])
		for position, rawTool := range anySlice(delta["tool_calls"]) {
			call := anyMap(rawTool)
			key := numberKey(call["index"], position)
			tool := a.tool(key)
			tool.ID = stringValue(call["id"], tool.ID)
			tool.Type = stringValue(call["type"], tool.Type)
			function := anyMap(call["function"])
			tool.Name = stringValue(function["name"], tool.Name)
			if arguments, ok := function["arguments"].(string); ok {
				tool.Arguments.WriteString(arguments)
			}
		}
		a.finishReason = stringValue(choice["finish_reason"], a.finishReason)
	}
}

func (a *conversationAccumulator) consumeOpenAIResponses(event sseEvent) {
	body := decodeObject(event.Data)
	eventType := firstNonEmpty(event.Name, stringValue(body["type"], ""))
	switch eventType {
	case "response.output_text.delta":
		a.text.WriteString(stringValue(body["delta"], ""))
	case "response.output_text.done":
		if a.text.Len() == 0 {
			a.text.WriteString(stringValue(body["text"], ""))
		}
	case "response.output_item.added":
		item := anyMap(body["item"])
		if stringValue(item["type"], "") == "function_call" {
			key := firstNonEmpty(stringValue(item["id"], ""), stringValue(item["call_id"], ""), numberKey(body["output_index"], 0))
			tool := a.tool(key)
			tool.ID = firstNonEmpty(stringValue(item["call_id"], ""), stringValue(item["id"], ""), tool.ID)
			tool.Name = stringValue(item["name"], tool.Name)
			if value, ok := item["arguments"].(string); ok {
				tool.Arguments.WriteString(value)
			}
		}
	case "response.function_call_arguments.delta":
		key := firstNonEmpty(stringValue(body["item_id"], ""), stringValue(body["call_id"], ""), numberKey(body["output_index"], 0))
		a.tool(key).Arguments.WriteString(stringValue(body["delta"], ""))
	case "response.function_call_arguments.done":
		key := firstNonEmpty(stringValue(body["item_id"], ""), stringValue(body["call_id"], ""), numberKey(body["output_index"], 0))
		tool := a.tool(key)
		if tool.Arguments.Len() == 0 {
			tool.Arguments.WriteString(stringValue(body["arguments"], ""))
		}
	case "response.completed":
		if response := anyMap(body["response"]); response != nil {
			a.replaceFromResponsesObject(response)
			a.finishReason = stringValue(response["status"], a.finishReason)
		}
		a.complete = true
	}
}

func (a *conversationAccumulator) replaceFromResponsesObject(response map[string]any) {
	var visible strings.Builder
	var tools []map[string]any
	for _, rawItem := range anySlice(response["output"]) {
		item := anyMap(rawItem)
		switch stringValue(item["type"], "") {
		case "message":
			for _, rawContent := range anySlice(item["content"]) {
				content := anyMap(rawContent)
				if stringValue(content["type"], "") == "output_text" {
					visible.WriteString(stringValue(content["text"], ""))
				}
			}
		case "function_call":
			tools = append(tools, item)
		}
	}
	if visible.Len() > 0 {
		a.text.Reset()
		a.text.WriteString(visible.String())
	}
	if len(tools) > 0 {
		a.tools = make(map[string]*toolCallBuilder)
		a.toolOrder = nil
		for index, item := range tools {
			key := firstNonEmpty(stringValue(item["id"], ""), stringValue(item["call_id"], ""), strconv.Itoa(index))
			tool := a.tool(key)
			tool.ID = firstNonEmpty(stringValue(item["call_id"], ""), stringValue(item["id"], ""))
			tool.Name = stringValue(item["name"], "")
			tool.Arguments.WriteString(stringValue(item["arguments"], ""))
		}
	}
}

func (a *conversationAccumulator) consumeAnthropic(event sseEvent) {
	body := decodeObject(event.Data)
	eventType := firstNonEmpty(event.Name, stringValue(body["type"], ""))
	switch eventType {
	case "content_block_start":
		index := numberKey(body["index"], 0)
		block := anyMap(body["content_block"])
		switch stringValue(block["type"], "") {
		case "text":
			a.text.WriteString(stringValue(block["text"], ""))
		case "tool_use":
			tool := a.tool(index)
			tool.ID = stringValue(block["id"], "")
			tool.Name = stringValue(block["name"], "")
			tool.Input = block["input"]
		}
	case "content_block_delta":
		index := numberKey(body["index"], 0)
		delta := anyMap(body["delta"])
		switch stringValue(delta["type"], "") {
		case "text_delta":
			a.text.WriteString(stringValue(delta["text"], ""))
		case "input_json_delta":
			tool := a.tool(index)
			tool.Input = nil
			tool.Arguments.WriteString(stringValue(delta["partial_json"], ""))
		}
	case "message_delta":
		a.finishReason = stringValue(anyMap(body["delta"])["stop_reason"], a.finishReason)
	case "message_stop":
		a.complete = true
	}
}

func (a *conversationAccumulator) consumeGemini(event sseEvent) {
	body := decodeObject(event.Data)
	for _, rawCandidate := range anySlice(body["candidates"]) {
		candidate := anyMap(rawCandidate)
		content := anyMap(candidate["content"])
		for index, rawPart := range anySlice(content["parts"]) {
			part := anyMap(rawPart)
			if text, ok := part["text"].(string); ok {
				a.text.WriteString(text)
			}
			if functionCall := anyMap(part["functionCall"]); functionCall != nil {
				key := firstNonEmpty(stringValue(functionCall["id"], ""), strconv.Itoa(index))
				tool := a.tool(key)
				tool.ID = stringValue(functionCall["id"], "")
				tool.Name = stringValue(functionCall["name"], "")
				tool.Input = functionCall["args"]
			}
		}
		if finish := stringValue(candidate["finishReason"], ""); finish != "" {
			a.finishReason = finish
			a.complete = true
		}
	}
}

func (a *conversationAccumulator) appendVisibleContent(value any) {
	if text, ok := value.(string); ok {
		a.text.WriteString(text)
		return
	}
	for _, rawBlock := range anySlice(value) {
		block := anyMap(rawBlock)
		if blockType := stringValue(block["type"], ""); blockType == "text" || blockType == "output_text" {
			a.text.WriteString(stringValue(block["text"], ""))
		}
	}
}

func (a *conversationAccumulator) result(model string) (json.RawMessage, json.RawMessage, error) {
	tools := make([]archivedToolCall, 0, len(a.toolOrder))
	for _, key := range a.toolOrder {
		builder := a.tools[key]
		arguments := builder.Input
		if raw := builder.Arguments.String(); raw != "" {
			var decoded any
			if json.Unmarshal([]byte(raw), &decoded) == nil {
				arguments = decoded
			} else {
				arguments = raw
			}
		}
		tools = append(tools, archivedToolCall{
			ID: builder.ID, Type: firstNonEmpty(builder.Type, "function"),
			Name: builder.Name, Arguments: arguments,
		})
	}
	toolJSON, err := json.Marshal(tools)
	if err != nil {
		return nil, nil, err
	}
	finalJSON, err := json.Marshal(map[string]any{
		"protocol":       a.protocol.String(),
		"model":          model,
		"assistant_text": a.text.String(),
		"tool_calls":     tools,
		"finish_reason":  a.finishReason,
	})
	if err != nil {
		return nil, nil, err
	}
	return toolJSON, finalJSON, nil
}

func anyMap(value any) map[string]any {
	result, _ := value.(map[string]any)
	return result
}

func anySlice(value any) []any {
	result, _ := value.([]any)
	return result
}

func stringValue(value any, fallback string) string {
	if result, ok := value.(string); ok && result != "" {
		return result
	}
	return fallback
}

func numberKey(value any, fallback int) string {
	switch number := value.(type) {
	case float64:
		return strconv.Itoa(int(number))
	case json.Number:
		return number.String()
	case int:
		return strconv.Itoa(number)
	default:
		return strconv.Itoa(fallback)
	}
}

func (a *conversationAccumulator) String() string {
	return fmt.Sprintf("%s complete=%t text=%d tools=%d", a.protocol.String(), a.complete, a.text.Len(), len(a.tools))
}
