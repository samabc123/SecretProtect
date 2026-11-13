<template>
  <div class="setting-field">
    <label>
      {{ item.label }}
      <el-tag v-if="item.sensitive" size="small" type="info">
        {{ item.configured ? "已配置" : "未配置" }}
      </el-tag>
    </label>
    <el-switch
      v-if="item.kind === 'boolean'"
      :model-value="Boolean(modelValue)"
      @update:model-value="emit('update:modelValue', $event)"
    />
    <el-select
      v-else-if="item.options?.length"
      :model-value="modelValue"
      @update:model-value="emit('update:modelValue', $event)"
    >
      <el-option v-for="option in item.options" :key="option" :label="option" :value="option" />
    </el-select>
    <el-input
      v-else-if="item.key.includes('REPOSITORIES') || item.key.includes('FINGERPRINTS')"
      type="textarea"
      :model-value="modelValue"
      rows="4"
      @update:model-value="emit('update:modelValue', $event)"
    />
    <el-input
      v-else
      :model-value="modelValue"
      :type="item.sensitive ? 'password' : item.kind === 'number' ? 'number' : 'text'"
      :placeholder="item.sensitive ? '留空保持当前配置' : ''"
      @update:model-value="onInput"
    />
    <small>{{ item.description }}</small>
  </div>
</template>

<script setup>
const props = defineProps({
  item: { type: Object, required: true },
  modelValue: [String, Number, Boolean, Array],
});
const emit = defineEmits(["update:modelValue"]);

function onInput(value) {
  if (props.item.kind === "number" && value !== "") {
    emit("update:modelValue", Number(value));
    return;
  }
  emit("update:modelValue", value);
}
</script>
