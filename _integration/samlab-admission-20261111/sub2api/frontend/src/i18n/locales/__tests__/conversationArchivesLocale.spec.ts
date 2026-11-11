import { existsSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const enAdminIndexPath = resolve(root, 'en/admin/index.ts')
const zhAdminIndexPath = resolve(root, 'zh/admin/index.ts')
const enAdminConversationArchivesPath = resolve(root, 'en/admin/conversationArchives.ts')
const zhAdminConversationArchivesPath = resolve(root, 'zh/admin/conversationArchives.ts')
const enCommonPath = resolve(root, 'en/common.ts')
const zhCommonPath = resolve(root, 'zh/common.ts')

function readWhenPresent(path: string): string {
  return existsSync(path) ? readFileSync(path, 'utf8') : ''
}

describe('conversation archive locale wiring', () => {
  it('registers archive management strings in admin locale barrels', () => {
    expect(existsSync(enAdminConversationArchivesPath)).toBe(true)
    expect(existsSync(zhAdminConversationArchivesPath)).toBe(true)
    expect(readWhenPresent(enAdminIndexPath)).toContain("import conversationArchives from './conversationArchives'")
    expect(readWhenPresent(enAdminIndexPath)).toContain('...conversationArchives')
    expect(readWhenPresent(zhAdminIndexPath)).toContain("import conversationArchives from './conversationArchives'")
    expect(readWhenPresent(zhAdminIndexPath)).toContain('...conversationArchives')
  })

  it('adds archive navigation labels to both common locale files', () => {
    expect(readWhenPresent(enCommonPath)).toContain("conversationArchives: 'Archive Management'")
    expect(readWhenPresent(zhCommonPath)).toContain("conversationArchives: '归档管理'")
  })
})
