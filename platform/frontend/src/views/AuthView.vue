<template>
  <main class="protect-login">
    <header class="login-brand"><img :src="'/static/vue/branding/secret-radar-logo-80.png'" width="40" height="40" alt=""><strong>SecertProtect</strong></header>
    <section class="login-form" aria-labelledby="auth-title">
      <span class="eyebrow">实验室安全管理</span><h1 id="auth-title">管理员登录</h1>
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submit">
        <el-form-item label="用户名" prop="username"><el-input id="username" v-model="form.username" autocomplete="username" placeholder="请输入用户名" :prefix-icon="User" /></el-form-item>
        <el-form-item label="密码" prop="password"><el-input id="password" v-model="form.password" type="password" show-password autocomplete="current-password" placeholder="请输入密码" :prefix-icon="Lock" /></el-form-item>
        <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
        <el-button type="primary" native-type="submit" :loading="loading" :icon="Right">登录</el-button>
      </el-form>
      <p>仅限授权管理员访问</p>
    </section>
    <footer>SecertProtect / CrowdSec Engine</footer>
  </main>
</template>
<script setup>
import { reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Lock, User, Right } from "@element-plus/icons-vue";
import { useUserStore } from "../store/modules/user";
const route = useRoute(), router = useRouter(), store = useUserStore();
const formRef = ref(), loading = ref(false), error = ref("");
const form = reactive({ username: "", password: "" });
const rules = { username: [{ required: true, message: "请输入用户名" }], password: [{ required: true, message: "请输入密码" }] };
async function submit() {
  if (loading.value || !await formRef.value.validate().catch(() => false)) return;
  loading.value = true; error.value = "";
  try {
    await store.login(form);
    const next = String(route.query.next || "/");
    router.replace(store.user.needs_password_change ? "/settings#account" : next.startsWith("/") && !next.startsWith("//") ? next : "/");
  } catch (e) { error.value = e.response?.data?.detail || "登录失败，请稍后重试。"; }
  finally { loading.value = false; }
}
</script>
