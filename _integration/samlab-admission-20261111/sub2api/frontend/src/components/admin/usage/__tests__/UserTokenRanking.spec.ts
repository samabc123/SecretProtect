import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia } from 'pinia'

import UserTokenRanking from '../UserTokenRanking.vue'

const getUserBreakdown = vi.fn()
const getUserBreakdownExport = vi.fn()
const saveAs = vi.fn()
const jsonToSheet = vi.fn(() => ({}))
const aoaToSheet = vi.fn(() => ({}))
const appendSheet = vi.fn()
const writeWorkbook = vi.fn(() => new Uint8Array([1, 2, 3]))

vi.mock('@/api/admin/dashboard', () => ({
  getUserBreakdown: (...args: unknown[]) => getUserBreakdown(...args),
  getUserBreakdownExport: (...args: unknown[]) => getUserBreakdownExport(...args),
}))

vi.mock('file-saver', () => ({ saveAs: (...args: unknown[]) => saveAs(...args) }))

vi.mock('xlsx', () => ({
  utils: {
    book_new: () => ({}),
    json_to_sheet: (...args: unknown[]) => jsonToSheet(...args),
    aoa_to_sheet: (...args: unknown[]) => aoaToSheet(...args),
    book_append_sheet: (...args: unknown[]) => appendSheet(...args),
  },
  write: (...args: unknown[]) => writeWorkbook(...args),
}))

vi.mock('vue-i18n', async () => {
  const actual = await vi.importActual<typeof import('vue-i18n')>('vue-i18n')
  return {
    ...actual,
    useI18n: () => ({
      t: (key: string) => key,
    }),
  }
})

const item = (id: number, tokens: number) => ({
  user_id: id,
  email: `u${id}@test.com`,
  username: `user-${id}`,
  requests: 1,
  input_tokens: tokens,
  output_tokens: 0,
  cache_tokens: 0,
  total_tokens: tokens,
  actual_cost: 0.5,
})

const mountRanking = (props: Record<string, unknown> = {}) =>
  mount(UserTokenRanking, {
    props: {
      startDate: '2026-07-01',
      endDate: '2026-07-08',
      filters: {},
      ...props,
    },
    global: { plugins: [createPinia()], stubs: { Select: true, LoadingSpinner: true } },
  })

describe('UserTokenRanking', () => {
  beforeEach(() => {
    getUserBreakdown.mockReset()
    getUserBreakdown.mockResolvedValue({ users: [item(1, 100), item(2, 50)] })
    getUserBreakdownExport.mockReset()
    getUserBreakdownExport.mockResolvedValue({ users: [item(1, 100), item(2, 50)], start_date: '2026-07-01', end_date: '2026-07-08' })
    saveAs.mockReset()
    jsonToSheet.mockClear()
    aoaToSheet.mockClear()
    appendSheet.mockClear()
    writeWorkbook.mockClear()
  })

  it('loads on mount with shared filters and emits select-user with id + username on row click', async () => {
    const wrapper = mountRanking({ filters: { group_id: 3 }, model: 'claude-fable-5' })
    await flushPromises()

    expect(getUserBreakdown).toHaveBeenCalledWith(expect.objectContaining({
      group_id: 3,
      model: 'claude-fable-5',
      start_date: '2026-07-01',
      end_date: '2026-07-08',
      sort_by: 'total_tokens',
      limit: 50,
    }))

    const rows = wrapper.findAll('tbody tr')
    expect(rows).toHaveLength(2)

    await rows[0].trigger('click')
    expect(wrapper.text()).toContain('user-1')
    expect(wrapper.text()).not.toContain('u1@test.com')
    expect(wrapper.emitted('select-user')![0]).toEqual([1, 'user-1'])
  })

  it('reloads when shared filters change', async () => {
    const wrapper = mountRanking()
    await flushPromises()
    expect(getUserBreakdown).toHaveBeenCalledTimes(1)

    await wrapper.setProps({ filters: { user_id: 9 } })
    await flushPromises()

    expect(getUserBreakdown).toHaveBeenCalledTimes(2)
    expect(getUserBreakdown).toHaveBeenLastCalledWith(expect.objectContaining({ user_id: 9 }))
  })

  it('exports all matching rows with the current filters as xlsx', async () => {
    const wrapper = mountRanking({ filters: { group_id: 3 } })
    await flushPromises()

    const exportButton = wrapper.findAll('button').find((button) => button.text().includes('tokenRanking.export'))
    expect(exportButton).toBeTruthy()
    await exportButton!.trigger('click')
    await flushPromises()

    expect(getUserBreakdownExport).toHaveBeenCalledWith(expect.objectContaining({
      group_id: 3,
      start_date: '2026-07-01',
      end_date: '2026-07-08',
      sort_by: 'total_tokens',
    }))
    expect(jsonToSheet).toHaveBeenCalledWith([
      expect.objectContaining({ '排名': 1, '用户名': 'user-1', '用户 ID': 1, '总 Token': 100, '实际费用': 0.5 }),
      expect.objectContaining({ '排名': 2, '用户名': 'user-2', '用户 ID': 2, '总 Token': 50, '实际费用': 0.5 }),
    ], expect.objectContaining({
      header: ['排名', '用户名', '用户 ID', '请求数', '输入 Token', '输出 Token', '缓存 Token', '总 Token', '实际费用'],
    }))
    expect(appendSheet.mock.calls.map((call) => call[2])).toEqual(['用量排行', '筛选条件'])
    expect(writeWorkbook).toHaveBeenCalledWith(expect.anything(), { bookType: 'xlsx', type: 'array' })
    expect(saveAs).toHaveBeenCalledWith(expect.any(Blob), '用户用量排行_2026-07-01_2026-07-08.xlsx')
  })
})
