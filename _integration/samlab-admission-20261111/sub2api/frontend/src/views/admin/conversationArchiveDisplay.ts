import type { AgentTrajectoryEvent } from '@/api/admin'

export interface ArchiveRequestMessage {
  role: string
  text: string
  raw: unknown
  index: number
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function mediaPlaceholder(value: Record<string, unknown>): string | null {
  const type = typeof value.type === 'string' ? value.type.toLowerCase() : ''
  if (type.includes('image')) return '[image]'
  if (type.includes('audio')) return '[audio]'
  if (type.includes('file')) return '[file]'
  return null
}

export function contentText(value: unknown): string {
  if (value == null) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) {
    return value.map(contentText).filter(Boolean).join('\n')
  }
  if (!isRecord(value)) return String(value)

  const placeholder = mediaPlaceholder(value)
  if (placeholder) return placeholder

  for (const key of ['text', 'input_text', 'output_text']) {
    if (typeof value[key] === 'string') return value[key]
  }
  for (const key of ['content', 'parts']) {
    if (value[key] !== undefined) {
      const nested = contentText(value[key])
      if (nested) return nested
    }
  }
  return formatArchiveValue(value)
}

function pushMessage(
  messages: ArchiveRequestMessage[],
  role: unknown,
  raw: unknown,
  content: unknown,
) {
  const normalizedRole = typeof role === 'string' && role.trim() ? role.trim().toLowerCase() : 'unknown'
  messages.push({
    role: normalizedRole === 'model' ? 'assistant' : normalizedRole,
    text: contentText(content),
    raw,
    index: messages.length,
  })
}

function appendMessageArray(messages: ArchiveRequestMessage[], value: unknown) {
  if (!Array.isArray(value)) return
  for (const item of value) {
    if (isRecord(item)) {
      pushMessage(messages, item.role, item, item.content ?? item.parts ?? item.text ?? item)
    } else {
      pushMessage(messages, 'user', item, item)
    }
  }
}

export function extractArchiveRequestMessages(payload: unknown): ArchiveRequestMessage[] {
  if (!isRecord(payload)) return []
  const messages: ArchiveRequestMessage[] = []

  if (payload.system !== undefined) {
    pushMessage(messages, 'system', { system: payload.system }, payload.system)
  }
  if (payload.systemInstruction !== undefined) {
    pushMessage(messages, 'system', { systemInstruction: payload.systemInstruction }, payload.systemInstruction)
  }
  if (payload.instructions !== undefined) {
    pushMessage(messages, 'developer', { instructions: payload.instructions }, payload.instructions)
  }

  if (Array.isArray(payload.messages)) {
    appendMessageArray(messages, payload.messages)
  } else if (Array.isArray(payload.contents)) {
    appendMessageArray(messages, payload.contents)
  } else if (Array.isArray(payload.input)) {
    appendMessageArray(messages, payload.input)
  } else if (payload.input !== undefined) {
    pushMessage(messages, 'user', { input: payload.input }, payload.input)
  }

  return messages
}

export function formatArchiveValue(value: unknown): string {
  if (value == null || value === '') return '-'
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

const collapsedEventTypes = new Set([
  'tool_call',
  'tool_result',
  'command_execution',
  'file_change',
  'web_search',
])

export function shouldExpandTrajectoryEvent(event: AgentTrajectoryEvent): boolean {
  return !event.is_truncated && event.stored_bytes <= 64 * 1024 && !collapsedEventTypes.has(event.event_type)
}
