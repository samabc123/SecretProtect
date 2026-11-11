import { apiClient } from '../client'
import type { PaginatedResponse } from '@/types'

const archiveAPIBaseURL = '/_archive/api/v1'

export interface ConversationArchive {
  id: number
  user_id: number
  api_key_id?: number | null
  client_request_id?: string
  request_id: string
  protocol: string
  endpoint: string
  model: string
  request_payload?: unknown
  assistant_text?: string
  tool_calls?: unknown
  final_response?: unknown
  started_at: string
  completed_at: string
  duration_ms: number
  response_status: number
  privacy_entities: number
  expires_at: string
}

export interface ConversationArchiveQuery {
  page?: number
  page_size?: number
  user_id?: number
  api_key_id?: number
  model?: string
  protocol?: string
  start_time?: string
  end_time?: string
}

export interface ArchiveKeywordCount {
  keyword: string
  category: string
  occurrences: number
  conversations: number
  users: number
  weight: number
}

export interface ArchiveKeywordStats {
  days: number
  generated_at: string
  processed_documents: number
  eligible_documents: number
  covered_users: number
  items: ArchiveKeywordCount[]
}

export interface ArchiveKeywordQuery {
  days: 7 | 30
  limit?: number
}

export interface AgentTrajectory {
  id: number
  import_batch_id?: string
  user_id: number
  source: string
  source_thread_id: string
  source_turn_id: string
  trajectory_id: string
  title: string
  model: string
  model_provider: string
  status: string
  final_text?: string
  started_at: string
  completed_at: string
  duration_ms: number
  event_count: number
  tool_call_count: number
  privacy_entities: number
  content_sha256: string
  metadata?: unknown
  expires_at: string
}

export interface AgentTrajectoryEvent {
  id: number
  trajectory_id: number
  sequence_no: number
  source_item_id?: string
  event_type: string
  role?: string
  phase?: string
  tool_call_id?: string
  tool_name?: string
  content: unknown
  status?: string
  event_at: string
  privacy_entities: number
  original_bytes: number
  stored_bytes: number
  is_truncated: boolean
  content_sha256: string
  extra?: unknown
}

export interface AgentTrajectoryQuery {
  page?: number
  page_size?: number
  user_id?: number
  status?: string
  model?: string
  source?: string
  tool_name?: string
  start_time?: string
  end_time?: string
}

export interface AgentTrajectoryEventQuery {
  page?: number
  page_size?: number
}

export type ConversationArchiveListResponse = PaginatedResponse<ConversationArchive>
export type AgentTrajectoryListResponse = PaginatedResponse<AgentTrajectory>
export type AgentTrajectoryEventListResponse = PaginatedResponse<AgentTrajectoryEvent>

async function archiveGet<T>(path: string, params?: object): Promise<T> {
  const { data } = await apiClient.get<T>(path, {
    baseURL: archiveAPIBaseURL,
    params,
  })
  return data
}

export async function list(
  params: ConversationArchiveQuery
): Promise<ConversationArchiveListResponse> {
  return archiveGet<ConversationArchiveListResponse>('/conversation-archives', params)
}

export async function get(id: number): Promise<ConversationArchive> {
  return archiveGet<ConversationArchive>(`/conversation-archives/${id}`)
}

export async function listKeywords(params: ArchiveKeywordQuery): Promise<ArchiveKeywordStats> {
  return archiveGet<ArchiveKeywordStats>('/conversation-keywords', params)
}

export async function listTrajectories(
  params: AgentTrajectoryQuery
): Promise<AgentTrajectoryListResponse> {
  return archiveGet<AgentTrajectoryListResponse>('/agent-trajectories', params)
}

export async function getTrajectory(id: number): Promise<AgentTrajectory> {
  return archiveGet<AgentTrajectory>(`/agent-trajectories/${id}`)
}

export async function listTrajectoryEvents(
  id: number,
  params: AgentTrajectoryEventQuery = {}
): Promise<AgentTrajectoryEventListResponse> {
  return archiveGet<AgentTrajectoryEventListResponse>(`/agent-trajectories/${id}/events`, params)
}

export const conversationArchivesAPI = {
  list,
  get,
  listKeywords,
  listTrajectories,
  getTrajectory,
  listTrajectoryEvents,
}

export default conversationArchivesAPI
