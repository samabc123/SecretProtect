<template>
  <div :class="props.embedded ? 'space-y-4' : 'card'">
    <div
      v-if="!props.embedded"
      class="border-b border-gray-100 px-6 py-4 dark:border-dark-700"
    >
      <h2 class="text-lg font-medium text-gray-900 dark:text-white">
        {{ t('profile.accountIdentity') }}
      </h2>
    </div>
    <div :class="props.embedded ? '' : 'px-6 py-6'">
      <div class="space-y-4">
        <div v-if="props.embedded">
          <p class="text-sm font-semibold text-gray-900 dark:text-white">
            {{ t('profile.accountIdentity') }}
          </p>
        </div>
        <div class="rounded-2xl border border-gray-100 bg-gray-50/70 p-4 dark:border-dark-700 dark:bg-dark-900/30">
          <p class="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
            {{ t('profile.username') }}
          </p>
          <p data-testid="profile-username-readonly" class="mt-1 break-all font-medium text-gray-900 dark:text-white">
            {{ props.initialUsername || '-' }}
          </p>
        </div>
        <p class="text-sm text-gray-500 dark:text-gray-400">
          {{ t('profile.identityManagedByAdmin') }}
        </p>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'

const props = withDefaults(defineProps<{
  initialUsername: string
  embedded?: boolean
}>(), {
  embedded: false,
})

const { t } = useI18n()
</script>
