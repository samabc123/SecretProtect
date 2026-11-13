<template>
  <el-tag :type="tagType" :class="stateClass(value, kind)" size="small">
    {{ displayState(value, kind) }}
  </el-tag>
</template>
<script setup>
import { computed } from "vue";
import { displayState, stateClass, stateType } from "../utils/format";

const props = defineProps({
  value: [String, Number, Boolean],
  kind: { type: String, default: "" },
});

const tagType = computed(() => {
  const type = stateType(props.value, props.kind);
  return type.startsWith("state-risk-") ? "info" : type;
});
</script>
