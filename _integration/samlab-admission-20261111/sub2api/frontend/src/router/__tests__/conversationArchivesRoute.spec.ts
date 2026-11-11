import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const routerPath = resolve(dirname(fileURLToPath(import.meta.url)), '../index.ts')
const routerSource = readFileSync(routerPath, 'utf8')

describe('conversation archive admin route', () => {
  it('registers an admin-only conversation archive page', () => {
    expect(routerSource).toContain("path: '/admin/conversation-archives'")
    expect(routerSource).toContain("name: 'AdminConversationArchives'")
    expect(routerSource).toContain("component: () => import('@/views/admin/ConversationArchivesView.vue')")
    expect(routerSource).toContain('requiresAdmin: true')
    expect(routerSource).toContain("titleKey: 'admin.conversationArchives.title'")
    expect(routerSource).toContain("descriptionKey: 'admin.conversationArchives.description'")
  })
})
