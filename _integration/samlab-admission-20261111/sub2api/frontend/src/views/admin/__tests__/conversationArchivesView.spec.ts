import { existsSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const viewPath = resolve(dirname(fileURLToPath(import.meta.url)), '../ConversationArchivesView.vue')

describe('ConversationArchivesView source wiring', () => {
  it('exists and reads archive data through the admin archive API facade', () => {
    expect(existsSync(viewPath)).toBe(true)
    if (!existsSync(viewPath)) return

    const source = readFileSync(viewPath, 'utf8')
    expect(source).toContain('adminAPI.conversationArchives.list({')
    expect(source).toContain('adminAPI.conversationArchives.listTrajectories({')
    expect(source).toContain('adminAPI.conversationArchives.get(id)')
    expect(source).toContain('adminAPI.conversationArchives.getTrajectory(id)')
    expect(source).toContain('adminAPI.conversationArchives.listTrajectoryEvents')
    expect(source).toContain('adminAPI.conversationArchives.listKeywords({')
    expect(source).toContain("const activeTab = ref<ArchiveTab>('conversations')")
  })

  it('renders keyword insights as a read-only static snapshot', () => {
    const source = readFileSync(viewPath, 'utf8')

    expect(source).toContain("const keywordDays = ref<7 | 30>(30)")
    expect(source).toContain("v-if=\"activeTab === 'conversations'\"")
    expect(source).not.toContain('filters.keyword')
    expect(source).not.toContain('@click="applyKeyword')
    expect(source).toContain("t('admin.conversationArchives.insights.wordCloud')")
  })

  it('invalidates stale detail and event-page responses before mutating the visible record', () => {
    const source = readFileSync(viewPath, 'utf8')

    expect(source).toContain('const detailRequestSequence = ref(0)')
    expect(source).toContain('const eventsRequestSequence = ref(0)')
    expect(source).toContain("sequence !== detailRequestSequence.value || detailKind.value !== 'conversation'")
    expect(source).toContain("sequence !== detailRequestSequence.value || detailKind.value !== 'trajectory'")
    expect(source).toContain('trajectoryDetail.value?.id !== trajectoryId')
    expect(source).toContain('if (sequence === eventsRequestSequence.value) eventsLoading.value = false')
  })
})
