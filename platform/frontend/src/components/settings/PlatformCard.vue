<template><SettingCard :title="title" :subtitle="subtitle"><p class="platform-help">{{ helpText }}</p><SettingField v-for="key in [`${prefix}_API_BASE`,`${prefix}_TOKEN`,`${prefix}_REPOSITORIES`]" :key="key" :item="itemByKey(key)" :model-value="values[key]" @update:model-value="$emit('update-value', { key, value: $event })" /><template #footer><el-button type="primary" @click="$emit('save',[`${prefix}_API_BASE`,`${prefix}_TOKEN`,`${prefix}_REPOSITORIES`])">保存设置</el-button><el-button @click="$emit('test',prefix,'repository')">测试仓库</el-button><el-button v-if="prefix === 'GITEE'" @click="$emit('test',prefix,'token')">验证 Token</el-button></template></SettingCard></template>
<script setup>
import { computed } from "vue";
import SettingCard from "./SettingCard.vue"; import SettingField from "./SettingField.vue";
const props = defineProps({ title: String, prefix: String, values: Object, itemByKey: Function });
defineEmits(["save","test","update-value"]);
const helpText = computed(() => props.prefix === "GITHUB"
  ? "GitHub 平台专用验证器会调用官方 /user 身份端点。填写 GitHub Token、明确授权的 owner/repository 白名单，保存后点击“测试仓库”。"
  : "Gitee 平台专用验证器会调用官方用户身份端点。填写 Gitee Token、明确授权的 owner/repository 白名单，保存后可测试 Token 或仓库。");
</script>
<style scoped>
.platform-help {
  margin: 0 0 14px;
  color: #708096;
  font-size: 12px;
  line-height: 1.6;
}
</style>
