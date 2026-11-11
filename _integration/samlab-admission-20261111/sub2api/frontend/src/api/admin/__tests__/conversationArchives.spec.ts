import { beforeEach, describe, expect, it, vi } from 'vitest'

import { apiClient } from '../../client'

async function loadConversationArchivesModule() {
  const modulePromise = vi.importActual('../conversationArchives')
  await expect(modulePromise).resolves.toBeTruthy()
  return await modulePromise as Record<string, any>
}

describe('conversationArchivesAPI', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('queries the independent archive plugin instead of the Sub2API admin backend', async () => {
    const response = { items: [], total: 0, page: 1, page_size: 20, pages: 0 }
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: response } as never)

    const module = await loadConversationArchivesModule()
    const conversationArchivesAPI = module.default

    await expect(conversationArchivesAPI.list({ page: 1, page_size: 20 })).resolves.toEqual(response)
    expect(get).toHaveBeenCalledWith('/conversation-archives', {
      baseURL: '/_archive/api/v1',
      params: { page: 1, page_size: 20 },
    })
    expect(apiClient.getUri({
      baseURL: '/_archive/api/v1',
      url: '/conversation-archives',
    })).toBe('/_archive/api/v1/conversation-archives')
  })

  it('provides trajectory detail and paginated event endpoints', async () => {
    const get = vi.spyOn(apiClient, 'get')
      .mockResolvedValueOnce({ data: { id: 9 } } as never)
      .mockResolvedValueOnce({ data: { items: [], total: 0, page: 1, page_size: 200, pages: 0 } } as never)

    const module = await loadConversationArchivesModule()
    const conversationArchivesAPI = module.default

    await conversationArchivesAPI.getTrajectory(9)
    await conversationArchivesAPI.listTrajectoryEvents(9, { page: 1, page_size: 200 })

    expect(get).toHaveBeenNthCalledWith(1, '/agent-trajectories/9', {
      baseURL: '/_archive/api/v1',
      params: undefined,
    })
    expect(get).toHaveBeenNthCalledWith(2, '/agent-trajectories/9/events', {
      baseURL: '/_archive/api/v1',
      params: { page: 1, page_size: 200 },
    })
  })

  it('queries keyword statistics from the archive plugin', async () => {
    const response = {
      days: 30,
      generated_at: '2026-10-08T03:00:00Z',
      processed_documents: 120,
      eligible_documents: 90,
      covered_users: 12,
      items: [],
    }
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: response } as never)

    const module = await loadConversationArchivesModule()
    const conversationArchivesAPI = module.default

    await expect(conversationArchivesAPI.listKeywords({ days: 30, limit: 60 })).resolves.toEqual(response)
    expect(get).toHaveBeenCalledWith('/conversation-keywords', {
      baseURL: '/_archive/api/v1',
      params: { days: 30, limit: 60 },
    })
  })
})
