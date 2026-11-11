const weeklyReportPath = '/custom/weekly-report'
const fileCenterPath = '/custom/file-center'
const orderedCustomPaths = [weeklyReportPath, fileCenterPath]

export function buildAdminPersonalNavItems<T extends { path: string }>(items: T[], archiveItem: T): T[] {
  const firstCustomIndex = items.findIndex(item => item.path.startsWith('/custom/'))
  const insertIndex = firstCustomIndex >= 0 ? firstCustomIndex : items.length

  const pinnedItems = new Map(
    items
      .filter(item => orderedCustomPaths.includes(item.path))
      .map(item => [item.path, item] as const)
  )

  const orderedBlock = [
    archiveItem,
    ...orderedCustomPaths.flatMap(path => {
      const item = pinnedItems.get(path)
      return item ? [item] : []
    }),
  ]

  const remainingItems = items.filter(item => !orderedCustomPaths.includes(item.path))
  remainingItems.splice(insertIndex, 0, ...orderedBlock)
  return remainingItems
}
