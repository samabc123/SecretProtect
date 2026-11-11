<template><el-card shadow="never" class="settings-card"><template #header>当前运行状态</template><div class="status-strip"><div><span>连接器联网</span><strong>{{ runtime.network_enabled ? "开启" : "关闭" }}</strong></div><div><span>GitHub</span><strong>{{ runtime.github_ready ? "就绪" : "未就绪" }}</strong></div><div><span>Gitee</span><strong>{{ runtime.gitee_ready ? "就绪" : "未就绪" }}</strong></div><div><span>HMAC</span><strong>{{ runtime.hmac_configured ? "已配置" : "未配置" }}</strong></div></div></el-card></template>
<script setup>
defineProps({ runtime: { type: Object, default: () => ({}) } });
</script>
