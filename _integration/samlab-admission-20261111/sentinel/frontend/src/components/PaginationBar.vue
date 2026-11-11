<template>
  <div class="pagination-bar">
    <span class="pagination-summary">
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
});

defineEmits(["update:page", "update:pageSize"]);

const totalPages = computed(() => (
  props.total ? Math.max(1, Math.ceil(props.total / props.pageSize)) : 1
));
</script>
