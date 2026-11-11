import { describe, expect, it } from 'vitest'

import { buildAdminPersonalNavItems } from '../personalNavOrdering'

interface TestNavItem {
  path: string
  label: string
}

function item(path: string): TestNavItem {
  return { path, label: path }
}

describe('buildAdminPersonalNavItems', () => {
  it('inserts archive, weekly report, and file center as one ordered block at the first custom slot without dropping other custom items', () => {
    const items = [
      item('/keys'),
      item('/usage'),
      item('/custom/changelog'),
      item('/custom/weekly-report'),
      item('/custom/faq'),
      item('/custom/file-center'),
      item('/custom/help'),
    ]

    expect(buildAdminPersonalNavItems(items, item('/admin/conversation-archives')).map(entry => entry.path)).toEqual([
      '/keys',
      '/usage',
      '/admin/conversation-archives',
      '/custom/weekly-report',
      '/custom/file-center',
      '/custom/changelog',
      '/custom/faq',
      '/custom/help',
    ])
  })

  it('keeps only the existing priority items when weekly report is missing', () => {
    const items = [
      item('/keys'),
      item('/profile'),
      item('/custom/changelog'),
      item('/custom/file-center'),
      item('/custom/help'),
    ]

    expect(buildAdminPersonalNavItems(items, item('/admin/conversation-archives')).map(entry => entry.path)).toEqual([
      '/keys',
      '/profile',
      '/admin/conversation-archives',
      '/custom/file-center',
      '/custom/changelog',
      '/custom/help',
    ])
  })

  it('keeps only the existing priority items when file center is missing', () => {
    const items = [
      item('/keys'),
      item('/profile'),
      item('/custom/changelog'),
      item('/custom/weekly-report'),
      item('/custom/help'),
    ]

    expect(buildAdminPersonalNavItems(items, item('/admin/conversation-archives')).map(entry => entry.path)).toEqual([
      '/keys',
      '/profile',
      '/admin/conversation-archives',
      '/custom/weekly-report',
      '/custom/changelog',
      '/custom/help',
    ])
  })
})
