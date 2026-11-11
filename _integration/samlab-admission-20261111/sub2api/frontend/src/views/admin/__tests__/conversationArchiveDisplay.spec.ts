import { describe, expect, it, vi } from 'vitest'

async function loadConversationArchiveDisplayModule() {
  const modulePromise = vi.importActual('../conversationArchiveDisplay')
  await expect(modulePromise).resolves.toBeTruthy()
  return await modulePromise as Record<string, any>
}

describe('conversation archive request display', () => {
  it('extracts developer context and user questions from OpenAI Responses payloads', async () => {
    const { extractArchiveRequestMessages } = await loadConversationArchiveDisplayModule()
    const messages = extractArchiveRequestMessages({
      instructions: 'Answer briefly',
      input: [
        { role: 'user', content: [{ type: 'input_text', text: 'What is the result?' }] },
        { role: 'assistant', content: [{ type: 'output_text', text: 'Earlier answer' }] },
      ],
    })

    expect(messages.map((item: { role: string; text: string }) => [item.role, item.text])).toEqual([
      ['developer', 'Answer briefly'],
      ['user', 'What is the result?'],
      ['assistant', 'Earlier answer'],
    ])
  })

  it('extracts Anthropic system prompts and Gemini content parts', async () => {
    const { extractArchiveRequestMessages } = await loadConversationArchiveDisplayModule()

    expect(extractArchiveRequestMessages({
      system: [{ type: 'text', text: 'System context' }],
      messages: [{ role: 'user', content: [{ type: 'text', text: 'User question' }] }],
    }).map((item: { text: string }) => item.text)).toEqual(['System context', 'User question'])

    expect(extractArchiveRequestMessages({
      contents: [{ role: 'user', parts: [{ text: 'Gemini question' }] }],
    })[0]?.text).toBe('Gemini question')
  })

  it('replaces media content with a non-sensitive placeholder', async () => {
    const { contentText } = await loadConversationArchiveDisplayModule()
    expect(contentText({ type: 'input_image', image_url: 'data:image/png;base64,secret' })).toBe('[image]')
  })
})

describe('agent trajectory event expansion', () => {
  const event = (overrides: Record<string, any>) => ({
    id: 1,
    trajectory_id: 2,
    sequence_no: 1,
    event_type: 'assistant_message',
    content: 'done',
    event_at: '2026-08-07T00:00:00Z',
    privacy_entities: 0,
    original_bytes: 4,
    stored_bytes: 4,
    is_truncated: false,
    content_sha256: 'hash',
    ...overrides,
  })

  it('expands small messages but collapses tools, diffs, large and truncated events', async () => {
    const { shouldExpandTrajectoryEvent } = await loadConversationArchiveDisplayModule()

    expect(shouldExpandTrajectoryEvent(event({}))).toBe(true)
    expect(shouldExpandTrajectoryEvent(event({ event_type: 'tool_result' }))).toBe(false)
    expect(shouldExpandTrajectoryEvent(event({ stored_bytes: 70 * 1024 }))).toBe(false)
    expect(shouldExpandTrajectoryEvent(event({ is_truncated: true }))).toBe(false)
  })
})
