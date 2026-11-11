<template>
  <AppLayout>
    <div class="space-y-4">
      <div class="card p-2">
        <div class="flex flex-wrap gap-2" role="tablist" :aria-label="t('admin.conversationArchives.tabs.label')">
          <button
            v-for="tab in tabs"
            :key="tab.value"
            type="button"
            role="tab"
            class="rounded-lg px-4 py-2 text-sm font-semibold transition-colors"
            :class="activeTab === tab.value
              ? 'bg-primary-600 text-white shadow-sm'
              : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-dark-700'"
            :aria-selected="activeTab === tab.value"
            @click="switchTab(tab.value)"
          >
            {{ tab.label }}
          </button>
        </div>
      </div>

      <section
        v-if="activeTab === 'conversations'"
        class="relative overflow-hidden rounded-2xl border border-blue-100 bg-gradient-to-br from-white via-blue-50/60 to-indigo-50 p-5 shadow-sm dark:border-dark-600 dark:from-dark-800 dark:via-dark-800 dark:to-indigo-950/30 sm:p-6"
      >
        <div class="pointer-events-none absolute -right-20 -top-28 h-72 w-72 rounded-full bg-blue-300/20 blur-3xl dark:bg-blue-700/10"></div>
        <div class="relative">
          <div class="flex flex-wrap items-start justify-between gap-4">
            <div>
              <div class="flex items-center gap-2">
                <span class="flex h-9 w-9 items-center justify-center rounded-xl bg-blue-600 text-white shadow-sm shadow-blue-200 dark:shadow-none">
                  <Icon name="sparkles" size="sm" />
                </span>
                <div>
                  <h2 class="text-lg font-bold text-gray-900 dark:text-white">
                    {{ t('admin.conversationArchives.insights.title') }}
                  </h2>
                  <p class="text-sm text-gray-500 dark:text-gray-400">
                    {{ t('admin.conversationArchives.insights.description') }}
                  </p>
                </div>
              </div>
            </div>
            <div class="flex rounded-xl bg-blue-100/70 p-1 dark:bg-dark-700">
              <button
                v-for="days in keywordDayOptions"
                :key="days"
                type="button"
                class="rounded-lg px-3 py-1.5 text-sm font-semibold transition-all"
                :class="keywordDays === days
                  ? 'bg-white text-blue-700 shadow-sm dark:bg-dark-600 dark:text-blue-300'
                  : 'text-gray-500 hover:text-blue-700 dark:text-gray-400 dark:hover:text-blue-300'"
                @click="changeKeywordDays(days)"
              >
                {{ t(`admin.conversationArchives.insights.days${days}`) }}
              </button>
              <button
                type="button"
                class="ml-1 rounded-lg px-3 py-1.5 text-sm font-medium text-gray-500 transition-colors hover:bg-white hover:text-blue-700 dark:text-gray-400 dark:hover:bg-dark-600 dark:hover:text-blue-300"
                :disabled="keywordLoading"
                @click="fetchKeywordStats"
              >
                {{ t('admin.conversationArchives.insights.reload') }}
              </button>
            </div>
          </div>

          <div class="mt-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
            <div v-for="metric in keywordMetrics" :key="metric.label" class="rounded-xl border border-white/80 bg-white/80 p-3.5 backdrop-blur-sm dark:border-dark-600 dark:bg-dark-800/80">
              <div class="text-2xl font-bold text-gray-900 dark:text-white">{{ metric.value }}</div>
              <div class="mt-1 text-xs font-medium text-gray-500 dark:text-gray-400">{{ metric.label }}</div>
            </div>
          </div>

          <div class="mt-4 grid gap-4 lg:grid-cols-[minmax(0,3fr)_minmax(260px,1fr)]">
            <div class="rounded-xl border border-white/80 bg-white/85 p-4 backdrop-blur-sm dark:border-dark-600 dark:bg-dark-800/85">
              <div class="mb-2 flex items-center justify-between gap-2">
                <h3 class="text-sm font-bold text-gray-800 dark:text-gray-100">{{ t('admin.conversationArchives.insights.wordCloud') }}</h3>
                <span v-if="keywordStats" class="text-xs text-gray-400">{{ formatTime(keywordStats.generated_at) }}</span>
              </div>
              <div v-if="keywordLoading && !keywordStats" class="flex min-h-64 items-center justify-center text-sm text-gray-400">
                {{ t('common.loading') }}
              </div>
              <div v-else-if="keywordItems.length" class="flex min-h-64 flex-wrap content-center items-center justify-center gap-x-4 gap-y-2 px-3 py-5">
                <span
                  v-for="item in keywordItems"
                  :key="item.keyword"
                  class="rounded-lg px-1.5 py-1 font-bold leading-none"
                  :class="keywordCategoryClass(item.category)"
                  :style="keywordStyle(item)"
                  :title="t('admin.conversationArchives.insights.keywordHint', { conversations: item.conversations, users: item.users })"
                >
                  {{ item.keyword }}
                </span>
              </div>
              <div v-else class="flex min-h-64 items-center justify-center text-sm text-gray-400">
                {{ t('admin.conversationArchives.insights.empty') }}
              </div>
            </div>

            <div class="rounded-xl border border-white/80 bg-white/85 p-4 backdrop-blur-sm dark:border-dark-600 dark:bg-dark-800/85">
              <div class="mb-4 flex items-center justify-between">
                <h3 class="text-sm font-bold text-gray-800 dark:text-gray-100">{{ t('admin.conversationArchives.insights.topTitle') }}</h3>
                <span class="text-xs text-gray-400">Top 10</span>
              </div>
              <div v-if="topKeywordItems.length" class="space-y-3">
                <div
                  v-for="(item, index) in topKeywordItems"
                  :key="item.keyword"
                  class="block w-full text-left"
                >
                  <span class="flex items-center justify-between gap-3 text-xs">
                    <span class="truncate font-semibold text-gray-700 dark:text-gray-200">{{ index + 1 }}. {{ item.keyword }}</span>
                    <span class="shrink-0 text-gray-400">{{ t('admin.conversationArchives.insights.conversationCount', { count: item.conversations }) }}</span>
                  </span>
                  <span class="mt-1.5 block h-1.5 overflow-hidden rounded-full bg-gray-100 dark:bg-dark-600">
                    <span class="block h-full rounded-full bg-gradient-to-r from-blue-400 to-indigo-500" :style="{ width: keywordRankWidth(item) }"></span>
                  </span>
                </div>
              </div>
              <div v-else class="py-16 text-center text-sm text-gray-400">{{ t('admin.conversationArchives.insights.empty') }}</div>
            </div>
          </div>
          <p v-if="keywordError" class="mt-3 text-xs text-amber-600 dark:text-amber-400">
            {{ keywordError }}
          </p>
        </div>
      </section>

      <TablePageLayout>
        <template #filters>
          <div class="card p-4 sm:p-6">
            <div class="flex flex-wrap items-end gap-4">
              <div class="w-full sm:w-36">
                <label class="input-label">{{ t('admin.conversationArchives.filters.userId') }}</label>
                <input v-model.trim="filters.userId" class="input" inputmode="numeric" @keyup.enter="search" />
              </div>
              <div v-if="activeTab === 'conversations'" class="w-full sm:w-36">
                <label class="input-label">{{ t('admin.conversationArchives.filters.apiKeyId') }}</label>
                <input v-model.trim="filters.apiKeyId" class="input" inputmode="numeric" @keyup.enter="search" />
              </div>
              <div class="w-full sm:min-w-52 sm:flex-1">
                <label class="input-label">{{ t('admin.conversationArchives.filters.model') }}</label>
                <input v-model.trim="filters.model" class="input" @keyup.enter="search" />
              </div>
              <div v-if="activeTab === 'conversations'" class="w-full sm:w-64">
                <label class="input-label">{{ t('admin.conversationArchives.filters.protocol') }}</label>
                <Select v-model="filters.protocol" :options="protocolOptions" @change="search" />
              </div>
              <div v-else class="w-full sm:w-44">
                <label class="input-label">{{ t('admin.conversationArchives.filters.status') }}</label>
                <Select v-model="filters.status" :options="statusOptions" @change="search" />
              </div>
              <div v-if="activeTab === 'trajectories'" class="w-full sm:w-44">
                <label class="input-label">{{ t('admin.conversationArchives.filters.source') }}</label>
                <input v-model.trim="filters.source" class="input" @keyup.enter="search" />
              </div>
              <div v-if="activeTab === 'trajectories'" class="w-full sm:w-44">
                <label class="input-label">{{ t('admin.conversationArchives.filters.toolName') }}</label>
                <input v-model.trim="filters.toolName" class="input" @keyup.enter="search" />
              </div>
              <div class="w-full sm:w-52">
                <label class="input-label">{{ t('admin.conversationArchives.filters.startTime') }}</label>
                <input v-model="filters.startTime" type="datetime-local" class="input" />
              </div>
              <div class="w-full sm:w-52">
                <label class="input-label">{{ t('admin.conversationArchives.filters.endTime') }}</label>
                <input v-model="filters.endTime" type="datetime-local" class="input" />
              </div>
              <div class="ml-auto flex gap-2">
                <button type="button" class="btn btn-secondary" :disabled="loading" @click="reset">
                  {{ t('common.reset') }}
                </button>
                <button type="button" class="btn btn-primary" :disabled="loading" @click="search">
                  <Icon name="search" size="sm" class="mr-1.5" />
                  {{ t('common.search') }}
                </button>
              </div>
            </div>
          </div>
        </template>

        <template #table>
          <DataTable
            v-if="activeTab === 'conversations'"
            :columns="conversationColumns"
            :data="archives"
            :loading="loading"
            row-key="id"
          >
            <template #cell-completed_at="{ value }">
              <span class="whitespace-nowrap text-gray-600 dark:text-gray-300">{{ formatTime(value) }}</span>
            </template>
            <template #cell-identity="{ row }">
              <div class="whitespace-nowrap">
                <div class="font-medium text-gray-900 dark:text-white">
                  {{ t('admin.conversationArchives.userNumber', { id: row.user_id }) }}
                </div>
                <div class="text-xs text-gray-400">
                  {{ t('admin.conversationArchives.keyNumber', { id: row.api_key_id ?? '-' }) }}
                </div>
              </div>
            </template>
            <template #cell-model="{ row }">
              <div class="max-w-64">
                <div class="truncate font-medium text-gray-900 dark:text-white" :title="row.model">
                  {{ row.model || '-' }}
                </div>
                <div class="mt-0.5 truncate font-mono text-xs text-gray-400" :title="row.protocol">
                  {{ protocolLabel(row.protocol) }}
                </div>
              </div>
            </template>
            <template #cell-archive_status="{ row }">
              <span class="inline-flex items-center gap-1.5 rounded-full bg-green-100 px-2.5 py-0.5 text-xs font-semibold text-green-700 dark:bg-green-900/30 dark:text-green-300">
                <span class="h-1.5 w-1.5 rounded-full bg-green-500"></span>
                HTTP {{ row.response_status }}
              </span>
            </template>
            <template #cell-duration_ms="{ value }">
              <span class="whitespace-nowrap text-gray-500 dark:text-gray-400">{{ value }} ms</span>
            </template>
            <template #cell-actions="{ row }">
              <button type="button" class="table-action" @click="openConversationDetail(row.id)">
                <Icon name="eye" size="sm" />
                {{ t('common.view') }}
              </button>
            </template>
            <template #empty>
              <EmptyState :text="t('admin.conversationArchives.emptyConversations')" />
            </template>
          </DataTable>

          <DataTable
            v-else
            :columns="trajectoryColumns"
            :data="trajectories"
            :loading="loading"
            row-key="id"
          >
            <template #cell-completed_at="{ value }">
              <span class="whitespace-nowrap text-gray-600 dark:text-gray-300">{{ formatTime(value) }}</span>
            </template>
            <template #cell-identity="{ row }">
              <div class="whitespace-nowrap font-medium text-gray-900 dark:text-white">
                {{ t('admin.conversationArchives.userNumber', { id: row.user_id }) }}
              </div>
            </template>
            <template #cell-trajectory_status="{ row }">
              <span :class="trajectoryStatusClass(row.status)">{{ row.status }}</span>
            </template>
            <template #cell-model="{ row }">
              <div class="max-w-64">
                <div class="truncate font-medium text-gray-900 dark:text-white" :title="row.model">
                  {{ row.model || '-' }}
                </div>
                <div class="mt-0.5 truncate text-xs text-gray-400" :title="row.model_provider">
                  {{ row.model_provider || '-' }}
                </div>
              </div>
            </template>
            <template #cell-source="{ row }">
              <div class="max-w-56">
                <div class="truncate text-gray-700 dark:text-gray-300" :title="row.source">{{ row.source }}</div>
                <div class="truncate font-mono text-xs text-gray-400" :title="row.source_turn_id">
                  {{ row.source_turn_id }}
                </div>
              </div>
            </template>
            <template #cell-event_counts="{ row }">
              <div class="whitespace-nowrap text-sm text-gray-600 dark:text-gray-300">
                {{ row.event_count }} / {{ row.tool_call_count }}
              </div>
            </template>
            <template #cell-actions="{ row }">
              <button type="button" class="table-action" @click="openTrajectoryDetail(row.id)">
                <Icon name="eye" size="sm" />
                {{ t('common.view') }}
              </button>
            </template>
            <template #empty>
              <EmptyState :text="t('admin.conversationArchives.emptyTrajectories')" />
            </template>
          </DataTable>
        </template>

        <template #pagination>
          <Pagination
            v-if="total > 0"
            :total="total"
            :page="page"
            :page-size="pageSize"
            @update:page="changePage"
            @update:pageSize="changePageSize"
          />
        </template>
      </TablePageLayout>
    </div>

    <BaseDialog
      :show="detailVisible"
      :title="detailTitle"
      width="wide"
      @close="closeDetail"
    >
      <div v-if="detailLoading" class="flex justify-center py-16">
        <div class="h-8 w-8 animate-spin rounded-full border-b-2 border-primary-600"></div>
      </div>

      <div v-else-if="conversationDetail" class="space-y-5 py-2">
        <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <SummaryItem
            v-for="item in conversationDetailSummary"
            :key="item.label"
            :label="item.label"
            :value="item.value"
          />
        </div>

        <MessageGroup
          :title="t('admin.conversationArchives.detail.userQuestions')"
          :messages="conversationUserMessages"
          :empty-text="t('admin.conversationArchives.detail.noUserQuestions')"
        />
        <MessageGroup
          v-if="conversationContextMessages.length"
          :title="t('admin.conversationArchives.detail.systemContext')"
          :messages="conversationContextMessages"
        />
        <MessageGroup
          v-if="conversationOtherMessages.length"
          :title="t('admin.conversationArchives.detail.otherMessages')"
          :messages="conversationOtherMessages"
        />
        <TextSection
          :title="t('admin.conversationArchives.detail.assistantText')"
          :value="conversationDetail.assistant_text"
        />
        <JsonSection
          :title="t('admin.conversationArchives.detail.toolCalls')"
          :value="conversationDetail.tool_calls"
        />
        <JsonSection
          :title="t('admin.conversationArchives.detail.finalResponse')"
          :value="conversationDetail.final_response"
        />
        <details class="rounded-lg border border-gray-200 p-3 dark:border-dark-600">
          <summary class="cursor-pointer text-sm font-semibold text-gray-600 dark:text-gray-300">
            {{ t('admin.conversationArchives.detail.requestPayload') }}
          </summary>
          <pre class="content-pre mt-3">{{ formatArchiveValue(conversationDetail.request_payload) }}</pre>
        </details>
      </div>

      <div v-else-if="trajectoryDetail" class="space-y-5 py-2">
        <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <SummaryItem
            v-for="item in trajectoryDetailSummary"
            :key="item.label"
            :label="item.label"
            :value="item.value"
          />
        </div>
        <TextSection
          :title="t('admin.conversationArchives.trajectory.finalAnswer')"
          :value="trajectoryDetail.final_text"
        />

        <section>
          <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h4 class="section-title">{{ t('admin.conversationArchives.trajectory.timeline') }}</h4>
            <span class="text-xs text-gray-400">
              {{ t('admin.conversationArchives.trajectory.eventProgress', { shown: trajectoryEvents.length, total: trajectoryEventsTotal }) }}
            </span>
          </div>
          <div v-if="trajectoryEvents.length" class="space-y-3">
            <article
              v-for="event in trajectoryEvents"
              :key="event.id"
              class="rounded-lg border-l-4 bg-gray-50 p-4 dark:bg-dark-900"
              :class="eventBorderClass(event.event_type)"
            >
              <div class="flex flex-wrap items-center gap-2">
                <span class="font-mono text-xs text-gray-400">#{{ event.sequence_no }}</span>
                <span class="font-semibold text-gray-900 dark:text-white">{{ event.event_type }}</span>
                <span v-if="event.role" class="event-pill">{{ event.role }}</span>
                <span v-if="event.phase" class="event-pill">{{ event.phase }}</span>
                <span v-if="event.tool_name" class="event-pill">{{ event.tool_name }}</span>
                <span v-if="event.status" class="event-pill">{{ event.status }}</span>
                <span v-if="event.is_truncated" class="event-pill event-pill-warning">
                  {{ t('admin.conversationArchives.trajectory.truncated') }}
                </span>
              </div>
              <div class="mt-1 text-xs text-gray-400">
                {{ formatTime(event.event_at) }} · {{ formatBytes(event.stored_bytes) }} / {{ formatBytes(event.original_bytes) }}
              </div>
              <details class="mt-3" :open="shouldExpandTrajectoryEvent(event)">
                <summary class="cursor-pointer text-sm font-medium text-primary-600 dark:text-primary-400">
                  {{ t('admin.conversationArchives.trajectory.redactedContent') }}
                </summary>
                <pre class="content-pre mt-2">{{ formatArchiveValue(event.content) }}</pre>
              </details>
            </article>
          </div>
          <div v-else class="rounded-lg bg-gray-50 p-6 text-center text-sm text-gray-400 dark:bg-dark-900">
            {{ t('admin.conversationArchives.trajectory.noEvents') }}
          </div>
          <div v-if="trajectoryEvents.length < trajectoryEventsTotal" class="mt-4 flex justify-center">
            <button type="button" class="btn btn-secondary" :disabled="eventsLoading" @click="loadMoreEvents">
              {{ eventsLoading ? t('common.loading') : t('admin.conversationArchives.trajectory.loadMore') }}
            </button>
          </div>
        </section>
      </div>
    </BaseDialog>
  </AppLayout>
</template>

<script setup lang="ts">
import { computed, defineComponent, h, onMounted, reactive, ref, type PropType } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  adminAPI,
  type AgentTrajectory,
  type AgentTrajectoryEvent,
  type ArchiveKeywordCount,
  type ArchiveKeywordStats,
  type ConversationArchive,
} from '@/api/admin'
import AppLayout from '@/components/layout/AppLayout.vue'
import TablePageLayout from '@/components/layout/TablePageLayout.vue'
import DataTable from '@/components/common/DataTable.vue'
import type { Column } from '@/components/common/types'
import Pagination from '@/components/common/Pagination.vue'
import Select from '@/components/common/Select.vue'
import BaseDialog from '@/components/common/BaseDialog.vue'
import Icon from '@/components/icons/Icon.vue'
import { useAppStore } from '@/stores'
import {
  extractArchiveRequestMessages,
  formatArchiveValue,
  shouldExpandTrajectoryEvent,
  type ArchiveRequestMessage,
} from './conversationArchiveDisplay'

type ArchiveTab = 'conversations' | 'trajectories'
type DetailKind = 'conversation' | 'trajectory' | null

const { t } = useI18n()
const appStore = useAppStore()
const activeTab = ref<ArchiveTab>('conversations')
const archives = ref<ConversationArchive[]>([])
const trajectories = ref<AgentTrajectory[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const requestSequence = ref(0)
const detailRequestSequence = ref(0)
const eventsRequestSequence = ref(0)

const detailVisible = ref(false)
const detailLoading = ref(false)
const detailKind = ref<DetailKind>(null)
const conversationDetail = ref<ConversationArchive | null>(null)
const trajectoryDetail = ref<AgentTrajectory | null>(null)
const trajectoryEvents = ref<AgentTrajectoryEvent[]>([])
const trajectoryEventsTotal = ref(0)
const trajectoryEventsPage = ref(1)
const eventsLoading = ref(false)
const eventsPageSize = 200
const keywordDayOptions = [7, 30] as const
const keywordDays = ref<7 | 30>(30)
const keywordStats = ref<ArchiveKeywordStats | null>(null)
const keywordLoading = ref(false)
const keywordError = ref('')

const filters = reactive({
  userId: '',
  apiKeyId: '',
  model: '',
  protocol: '',
  status: '',
  source: '',
  toolName: '',
  startTime: '',
  endTime: '',
})

const tabs = computed(() => [
  { value: 'conversations' as const, label: t('admin.conversationArchives.tabs.conversations') },
  { value: 'trajectories' as const, label: t('admin.conversationArchives.tabs.trajectories') },
])

const protocolOptions = computed(() => [
  { value: '', label: t('admin.conversationArchives.filters.allProtocols') },
  { value: 'openai_chat_completions', label: 'OpenAI Chat Completions' },
  { value: 'openai_responses', label: 'OpenAI Responses' },
  { value: 'anthropic_messages', label: 'Anthropic Messages' },
  { value: 'gemini_stream_generate_content', label: 'Gemini streamGenerateContent' },
])

const statusOptions = computed(() => [
  { value: '', label: t('admin.conversationArchives.filters.allStatuses') },
  { value: 'completed', label: 'completed' },
  { value: 'failed', label: 'failed' },
  { value: 'aborted', label: 'aborted' },
  { value: 'incomplete', label: 'incomplete' },
])

const keywordItems = computed(() => keywordStats.value?.items || [])
const topKeywordItems = computed(() => keywordItems.value.slice(0, 10))
const keywordWeightRange = computed(() => {
  const weights = keywordItems.value.map(item => Number(item.weight) || 0)
  return {
    min: weights.length ? Math.min(...weights) : 0,
    max: weights.length ? Math.max(...weights) : 1,
  }
})
const keywordMetrics = computed(() => [
  {
    label: t('admin.conversationArchives.insights.processed'),
    value: compactNumber(keywordStats.value?.processed_documents),
  },
  {
    label: t('admin.conversationArchives.insights.eligible'),
    value: compactNumber(keywordStats.value?.eligible_documents),
  },
  {
    label: t('admin.conversationArchives.insights.users'),
    value: compactNumber(keywordStats.value?.covered_users),
  },
  {
    label: t('admin.conversationArchives.insights.topics'),
    value: compactNumber(keywordItems.value.length),
  },
])

const conversationColumns = computed<Column[]>(() => [
  { key: 'completed_at', label: t('admin.conversationArchives.columns.completedAt') },
  { key: 'identity', label: t('admin.conversationArchives.columns.identity') },
  { key: 'model', label: t('admin.conversationArchives.columns.model') },
  { key: 'archive_status', label: t('admin.conversationArchives.columns.status') },
  { key: 'duration_ms', label: t('admin.conversationArchives.columns.duration') },
  { key: 'privacy_entities', label: t('admin.conversationArchives.columns.privacyEntities') },
  { key: 'actions', label: t('common.actions') },
])

const trajectoryColumns = computed<Column[]>(() => [
  { key: 'completed_at', label: t('admin.conversationArchives.columns.completedAt') },
  { key: 'identity', label: t('admin.conversationArchives.columns.identity') },
  { key: 'trajectory_status', label: t('admin.conversationArchives.columns.status') },
  { key: 'model', label: t('admin.conversationArchives.columns.model') },
  { key: 'source', label: t('admin.conversationArchives.columns.source') },
  { key: 'event_counts', label: t('admin.conversationArchives.columns.eventCounts') },
  { key: 'privacy_entities', label: t('admin.conversationArchives.columns.privacyEntities') },
  { key: 'actions', label: t('common.actions') },
])

const detailTitle = computed(() => detailKind.value === 'trajectory'
  ? t('admin.conversationArchives.trajectory.detailTitle')
  : t('admin.conversationArchives.detail.title'))

const conversationMessages = computed(() => extractArchiveRequestMessages(conversationDetail.value?.request_payload))
const conversationUserMessages = computed(() => conversationMessages.value.filter(item => item.role === 'user'))
const conversationContextMessages = computed(() => conversationMessages.value.filter(item => ['system', 'developer'].includes(item.role)))
const conversationOtherMessages = computed(() => conversationMessages.value.filter(item => !['user', 'system', 'developer'].includes(item.role)))

const conversationDetailSummary = computed(() => {
  if (!conversationDetail.value) return []
  const item = conversationDetail.value
  return [
    { label: t('admin.conversationArchives.columns.identity'), value: `#${item.user_id} / #${item.api_key_id ?? '-'}` },
    { label: t('admin.conversationArchives.columns.model'), value: item.model || '-' },
    { label: t('admin.conversationArchives.filters.protocol'), value: protocolLabel(item.protocol) },
    { label: t('admin.conversationArchives.columns.completedAt'), value: formatTime(item.completed_at) },
    { label: t('admin.conversationArchives.detail.requestId'), value: item.client_request_id || item.request_id },
    { label: t('admin.conversationArchives.detail.endpoint'), value: item.endpoint },
    { label: t('admin.conversationArchives.columns.duration'), value: `${item.duration_ms} ms / HTTP ${item.response_status}` },
    { label: t('admin.conversationArchives.detail.expiresAt'), value: formatTime(item.expires_at) },
  ]
})

const trajectoryDetailSummary = computed(() => {
  if (!trajectoryDetail.value) return []
  const item = trajectoryDetail.value
  return [
    { label: t('admin.conversationArchives.filters.userId'), value: `#${item.user_id}` },
    { label: t('admin.conversationArchives.filters.status'), value: item.status },
    { label: t('admin.conversationArchives.columns.model'), value: item.model || '-' },
    { label: t('admin.conversationArchives.filters.source'), value: item.source },
    { label: t('admin.conversationArchives.trajectory.completedAt'), value: formatTime(item.completed_at) },
    { label: t('admin.conversationArchives.trajectory.events'), value: String(item.event_count) },
    { label: t('admin.conversationArchives.trajectory.toolCalls'), value: String(item.tool_call_count) },
    { label: t('admin.conversationArchives.detail.expiresAt'), value: formatTime(item.expires_at) },
  ]
})

const SummaryItem = defineComponent({
  props: {
    label: { type: String, required: true },
    value: { type: String, required: true },
  },
  setup(props) {
    return () => h('div', { class: 'rounded-lg bg-gray-50 p-3 dark:bg-dark-900' }, [
      h('div', { class: 'text-xs font-medium text-gray-400' }, props.label),
      h('div', { class: 'mt-1 break-all text-sm font-semibold text-gray-900 dark:text-white' }, props.value),
    ])
  },
})

const TextSection = defineComponent({
  props: {
    title: { type: String, required: true },
    value: { type: String, default: '' },
  },
  setup(props) {
    return () => h('section', {}, [
      h('h4', { class: 'section-title mb-2' }, props.title),
      h('div', { class: 'content-text' }, props.value || '-'),
    ])
  },
})

const JsonSection = defineComponent({
  props: {
    title: { type: String, required: true },
    value: { type: [Object, Array, String, Number, Boolean] as PropType<any>, default: null },
  },
  setup(props) {
    return () => h('section', {}, [
      h('h4', { class: 'section-title mb-2' }, props.title),
      h('pre', { class: 'content-pre' }, formatArchiveValue(props.value)),
    ])
  },
})

const MessageGroup = defineComponent({
  props: {
    title: { type: String, required: true },
    messages: { type: Array as () => ArchiveRequestMessage[], required: true },
    emptyText: { type: String, default: '' },
  },
  setup(props) {
    return () => h('section', {}, [
      h('h4', { class: 'section-title mb-2' }, props.title),
      props.messages.length
        ? h('div', { class: 'space-y-2' }, props.messages.map(message => h('article', {
          class: 'rounded-lg border border-gray-200 bg-white p-3 dark:border-dark-600 dark:bg-dark-800',
        }, [
          h('div', { class: 'mb-2 text-xs font-bold uppercase text-gray-400' }, `${message.role} #${message.index + 1}`),
          h('div', { class: 'whitespace-pre-wrap break-words text-sm leading-6 text-gray-700 dark:text-gray-300' }, message.text || '-'),
        ])))
        : h('div', { class: 'content-text text-gray-400' }, props.emptyText || '-'),
    ])
  },
})

const EmptyState = defineComponent({
  props: { text: { type: String, required: true } },
  setup(props) {
    return () => h('div', { class: 'flex flex-col items-center py-10 text-gray-400' }, [
      h(Icon, { name: 'database', size: 'xl', class: 'mb-3 h-12 w-12' }),
      h('p', { class: 'text-sm font-medium' }, props.text),
    ])
  },
})

function numberFilter(value: string): number | undefined {
  if (!value) return undefined
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed > 0 ? parsed : undefined
}

function toISO(value: string): string | undefined {
  if (!value) return undefined
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? undefined : date.toISOString()
}

function archiveErrorMessage(error: unknown): string {
  const candidate = error as { status?: number; message?: string }
  if (candidate?.status === 403) return t('admin.conversationArchives.forbidden')
  if (candidate?.status === 401) return t('admin.conversationArchives.unauthorized')
  return candidate?.message || t('admin.conversationArchives.loadFailed')
}

async function fetchItems() {
  const sequence = ++requestSequence.value
  loading.value = true
  try {
    if (activeTab.value === 'conversations') {
      const result = await adminAPI.conversationArchives.list({
        page: page.value,
        page_size: pageSize.value,
        user_id: numberFilter(filters.userId),
        api_key_id: numberFilter(filters.apiKeyId),
        model: filters.model || undefined,
        protocol: filters.protocol || undefined,
        start_time: toISO(filters.startTime),
        end_time: toISO(filters.endTime),
      })
      if (sequence !== requestSequence.value) return
      archives.value = result.items
      total.value = result.total
    } else {
      const result = await adminAPI.conversationArchives.listTrajectories({
        page: page.value,
        page_size: pageSize.value,
        user_id: numberFilter(filters.userId),
        model: filters.model || undefined,
        status: filters.status || undefined,
        source: filters.source || undefined,
        tool_name: filters.toolName || undefined,
        start_time: toISO(filters.startTime),
        end_time: toISO(filters.endTime),
      })
      if (sequence !== requestSequence.value) return
      trajectories.value = result.items
      total.value = result.total
    }
  } catch (error) {
    if (sequence === requestSequence.value) appStore.showError(archiveErrorMessage(error))
  } finally {
    if (sequence === requestSequence.value) loading.value = false
  }
}

function switchTab(tab: ArchiveTab) {
  if (tab === activeTab.value) return
  activeTab.value = tab
  page.value = 1
  total.value = 0
  fetchItems()
  if (tab === 'conversations' && !keywordStats.value) fetchKeywordStats()
}

function search() {
  page.value = 1
  fetchItems()
}

function reset() {
  Object.assign(filters, {
    userId: '',
    apiKeyId: '',
    model: '',
    protocol: '',
    status: '',
    source: '',
    toolName: '',
    startTime: '',
    endTime: '',
  })
  search()
}

async function fetchKeywordStats() {
  if (keywordLoading.value) return
  keywordLoading.value = true
  keywordError.value = ''
  try {
    keywordStats.value = await adminAPI.conversationArchives.listKeywords({
      days: keywordDays.value,
      limit: 60,
    })
  } catch (error) {
    keywordError.value = archiveErrorMessage(error)
  } finally {
    keywordLoading.value = false
  }
}

function changeKeywordDays(days: 7 | 30) {
  if (keywordDays.value === days) return
  keywordDays.value = days
  fetchKeywordStats()
}

function keywordStyle(item: ArchiveKeywordCount): Record<string, string> {
  const { min, max } = keywordWeightRange.value
  const ratio = max === min ? 0.5 : (Number(item.weight) - min) / (max - min)
  return { fontSize: `${0.95 + Math.max(0, Math.min(1, ratio)) * 1.15}rem` }
}

function keywordRankWidth(item: ArchiveKeywordCount): string {
  const max = Number(topKeywordItems.value[0]?.weight) || 1
  return `${Math.max(8, (Number(item.weight) / max) * 100).toFixed(1)}%`
}

function keywordCategoryClass(category: string): string {
  const colors: Record<string, string> = {
    research: 'text-blue-700 dark:text-blue-300',
    literature: 'text-violet-700 dark:text-violet-300',
    experiment: 'text-orange-700 dark:text-orange-300',
    model: 'text-sky-700 dark:text-sky-300',
    data: 'text-emerald-700 dark:text-emerald-300',
    analysis: 'text-teal-700 dark:text-teal-300',
    writing: 'text-pink-700 dark:text-pink-300',
    code: 'text-indigo-700 dark:text-indigo-300',
    engineering: 'text-slate-700 dark:text-slate-300',
  }
  return colors[category] || 'text-gray-700 dark:text-gray-300'
}

function compactNumber(value: number | undefined): string {
  return new Intl.NumberFormat(undefined, {
    notation: Number(value) >= 10000 ? 'compact' : 'standard',
    maximumFractionDigits: 1,
  }).format(Number(value) || 0)
}

function changePage(value: number) {
  page.value = value
  fetchItems()
}

function changePageSize(value: number) {
  pageSize.value = value
  page.value = 1
  fetchItems()
}

function closeDetail() {
  detailRequestSequence.value += 1
  eventsRequestSequence.value += 1
  detailVisible.value = false
  detailLoading.value = false
  eventsLoading.value = false
  detailKind.value = null
  conversationDetail.value = null
  trajectoryDetail.value = null
  trajectoryEvents.value = []
  trajectoryEventsTotal.value = 0
}

async function openConversationDetail(id: number) {
  const sequence = ++detailRequestSequence.value
  eventsRequestSequence.value += 1
  detailVisible.value = true
  detailLoading.value = true
  eventsLoading.value = false
  detailKind.value = 'conversation'
  conversationDetail.value = null
  try {
    const item = await adminAPI.conversationArchives.get(id)
    if (sequence !== detailRequestSequence.value || detailKind.value !== 'conversation' || !detailVisible.value) return
    conversationDetail.value = item
  } catch (error) {
    if (sequence !== detailRequestSequence.value) return
    closeDetail()
    appStore.showError(archiveErrorMessage(error))
  } finally {
    if (sequence === detailRequestSequence.value) detailLoading.value = false
  }
}

async function openTrajectoryDetail(id: number) {
  const sequence = ++detailRequestSequence.value
  eventsRequestSequence.value += 1
  detailVisible.value = true
  detailLoading.value = true
  eventsLoading.value = false
  detailKind.value = 'trajectory'
  trajectoryDetail.value = null
  trajectoryEvents.value = []
  trajectoryEventsTotal.value = 0
  trajectoryEventsPage.value = 1
  try {
    const [item, events] = await Promise.all([
      adminAPI.conversationArchives.getTrajectory(id),
      adminAPI.conversationArchives.listTrajectoryEvents(id, { page: 1, page_size: eventsPageSize }),
    ])
    if (sequence !== detailRequestSequence.value || detailKind.value !== 'trajectory' || !detailVisible.value) return
    trajectoryDetail.value = item
    trajectoryEvents.value = events.items
    trajectoryEventsTotal.value = events.total
  } catch (error) {
    if (sequence !== detailRequestSequence.value) return
    closeDetail()
    appStore.showError(archiveErrorMessage(error))
  } finally {
    if (sequence === detailRequestSequence.value) detailLoading.value = false
  }
}

async function loadMoreEvents() {
  if (!trajectoryDetail.value || eventsLoading.value) return
  const trajectoryId = trajectoryDetail.value.id
  const sequence = ++eventsRequestSequence.value
  eventsLoading.value = true
  const nextPage = trajectoryEventsPage.value + 1
  try {
    const events = await adminAPI.conversationArchives.listTrajectoryEvents(trajectoryId, {
      page: nextPage,
      page_size: eventsPageSize,
    })
    if (
      sequence !== eventsRequestSequence.value
      || detailKind.value !== 'trajectory'
      || trajectoryDetail.value?.id !== trajectoryId
      || !detailVisible.value
    ) return
    trajectoryEvents.value.push(...events.items)
    trajectoryEventsPage.value = nextPage
    trajectoryEventsTotal.value = events.total
  } catch (error) {
    if (sequence === eventsRequestSequence.value && trajectoryDetail.value?.id === trajectoryId) {
      appStore.showError(archiveErrorMessage(error))
    }
  } finally {
    if (sequence === eventsRequestSequence.value) eventsLoading.value = false
  }
}

function protocolLabel(protocol: string): string {
  return protocolOptions.value.find(option => option.value === protocol)?.label || protocol
}

function formatTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '0 B'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`
}

function trajectoryStatusClass(status: string): string {
  const base = 'inline-flex rounded-full px-2.5 py-0.5 text-xs font-semibold'
  if (status === 'completed') return `${base} bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300`
  if (status === 'failed') return `${base} bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300`
  if (status === 'aborted') return `${base} bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300`
  return `${base} bg-gray-100 text-gray-600 dark:bg-dark-700 dark:text-gray-300`
}

function eventBorderClass(eventType: string): string {
  if (eventType === 'user_message') return 'border-green-500'
  if (eventType === 'assistant_message') return 'border-primary-500'
  if (['tool_call', 'tool_result', 'command_execution', 'file_change'].includes(eventType)) return 'border-amber-500'
  if (eventType.includes('failed') || eventType === 'error') return 'border-red-500'
  return 'border-gray-300 dark:border-dark-500'
}

onMounted(() => {
  fetchItems()
  fetchKeywordStats()
})
</script>

<style scoped>
.table-action {
  @apply inline-flex items-center gap-1 font-medium text-primary-600 hover:text-primary-700 dark:text-primary-400;
}

.section-title {
  @apply text-xs font-bold uppercase tracking-wide text-gray-400;
}

.content-text {
  @apply max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-gray-50 p-4 text-sm leading-6 text-gray-700 dark:bg-dark-900 dark:text-gray-300;
}

.content-pre {
  @apply max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-gray-50 p-4 font-mono text-xs leading-5 text-gray-700 dark:bg-dark-900 dark:text-gray-300;
}

.event-pill {
  @apply inline-flex rounded-full bg-gray-200 px-2 py-0.5 text-xs font-medium text-gray-600 dark:bg-dark-600 dark:text-gray-300;
}

.event-pill-warning {
  @apply bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300;
}
</style>
