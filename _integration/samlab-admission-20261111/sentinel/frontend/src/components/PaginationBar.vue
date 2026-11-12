<template>
  <div class="pagination-bar" :class="{ 'table-pagination': tableStyle }">
    <span class="pagination-summary">
      <template v-if="tableStyle && total">显示 {{ (page - 1) * pageSize + 1 }} 至 {{ Math.min(page * pageSize, total) }} · </template>
      共 {{ total }} 条<span v-if="total"> · 第 {{ page }} / {{ totalPages }} 页</span>
    </span>
    <el-pagination
      :current-page="page"
      :page-size="pageSize"
      :page-sizes="pageSizes"
      :total="total"
      :pager-count="5"
      size="small"
      background
      layout="sizes, first, prev, pager, next, last, jumper"
      @current-change="$emit('update:page', $event)"
      @size-change="$emit('update:pageSize', $event)"
    />
  </div>
</template>

<script setup>
import { computed } from "vue";

const props = defineProps({
  page: { type: Number, default: 1 },
  pageSize: { type: Number, default: 20 },
  total: { type: Number, default: 0 },
  pageSizes: { type: Array, default: () => [10, 20, 50] },
  tableStyle: Boolean,
});

defineEmits(["update:page", "update:pageSize"]);

const totalPages = computed(() => (
  props.total ? Math.max(1, Math.ceil(props.total / props.pageSize)) : 1
));
</script>

<style scoped>
.table-pagination { min-height: 64px; flex: 0 0 auto; padding: 12px 18px; border-top: 1px solid var(--line); background: #fff; }
@media (max-width: 900px) {
  .table-pagination { gap: 12px; padding-inline: 0; flex-wrap: wrap; }
  .table-pagination .pagination-summary { white-space: normal; line-height: 1.6; }
}
</style>
