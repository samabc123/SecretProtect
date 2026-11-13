<template>
  <main class="auth-page">
    <div class="auth-shell">
      <section class="auth-visual" aria-label="SecertProtect 实验室安全管理">
        <div class="visual-brand">
          <img :src="'/static/vue/branding/secret-radar-logo-80.png'" width="40" height="40" alt="">
          <span>SecertProtect</span>
        </div>
        <div class="visual-copy">
          <h1>让每一条证据<br>可追溯</h1>
          <p>实验室安全管理</p>
        </div>
        <div class="analysis-scene" aria-hidden="true">
          <div class="scene-grid scene-grid--left" />
          <div class="scene-grid scene-grid--right" />
          <div class="scene-window scene-window--main">
            <div class="window-dots"><span /><span /><span /></div>
            <div class="window-line window-line--long" />
            <div class="window-line window-line--medium" />
            <div class="window-line window-line--short" />
          </div>
          <div class="scene-chart">
            <span class="chart-bar chart-bar--one" />
            <span class="chart-bar chart-bar--two" />
            <span class="chart-bar chart-bar--three" />
            <span class="chart-line" />
            <i class="chart-dot chart-dot--one" />
            <i class="chart-dot chart-dot--two" />
            <i class="chart-dot chart-dot--three" />
          </div>
          <div class="scene-window scene-window--small">
            <div class="window-dots"><span /><span /><span /></div>
            <div class="window-line window-line--medium" />
            <div class="window-line window-line--short" />
          </div>
          <div class="scene-stack"><b /><b /><b /></div>
          <div class="scene-node scene-node--one">规</div>
          <div class="scene-node scene-node--two">证</div>
          <div class="scene-node scene-node--three">验</div>
        </div>
        <div class="visual-footer"><span>Samlab</span><span>实验室安全管理</span></div>
      </section>

      <section class="auth-panel" aria-labelledby="auth-title">
        <div class="panel-header"><span>你好，欢迎回来！</span></div>
        <div class="auth-heading">
          <h2 id="auth-title">Samlab 安全管理</h2>
          <p>管理员登录</p>
        </div>
        <el-form ref="formRef" class="auth-form" :model="form" :rules="rules" label-position="top" @submit.prevent="submit">
          <el-form-item label="用户名" prop="username">
            <el-input id="username" v-model="form.username" autocomplete="username" placeholder="请输入用户名" :prefix-icon="User" />
          </el-form-item>
          <el-form-item label="密码" prop="password">
            <el-input id="password" v-model="form.password" type="password" show-password autocomplete="current-password" placeholder="请输入密码" :prefix-icon="Lock" />
          </el-form-item>
          <el-alert v-if="error" class="auth-error" :title="error" type="error" :closable="false" show-icon />
          <el-button class="auth-submit" type="primary" native-type="submit" :loading="loading">登录</el-button>
        </el-form>
        <p class="auth-note">仅限授权管理员访问</p>
      </section>
    </div>
  </main>
</template>
<script setup>
import { reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Lock, User } from "@element-plus/icons-vue";
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

<style scoped>
.auth-page {
  --el-color-primary: #317bf3;
  --el-color-primary-light-3: #659cf6;
  --el-color-primary-dark-2: #2762c2;
  display: grid;
  place-items: center;
  min-height: 100dvh;
  padding: 28px;
  background: #eaf2ff;
  letter-spacing: 0;
}
.auth-page::before, .auth-page::after { display: none; }
.auth-shell {
  display: grid;
  grid-template-columns: minmax(0, 1.08fr) minmax(390px, .92fr);
  width: min(1240px, 100%);
  min-height: 760px;
  overflow: hidden;
  border: 1px solid rgba(173, 199, 228, .78);
  border-radius: 18px;
  background: #fff;
  box-shadow: 0 24px 60px rgba(57, 93, 146, .16);
}
.auth-visual {
  display: flex;
  flex-direction: column;
  min-width: 0;
  padding: 34px 42px 28px;
  overflow: hidden;
  background: #eaf2ff;
}
.visual-brand { display: flex; align-items: center; gap: 10px; color: #23416a; font-size: 20px; font-weight: 760; }
.visual-brand img { flex: 0 0 40px; }
.visual-copy { margin-top: 84px; }
.visual-copy h1 { margin: 0; color: #1e3556; font-size: 52px; font-weight: 780; line-height: 1.18; letter-spacing: 0; }
.visual-copy p { margin: 18px 0 0; color: #6c83a2; font-size: 14px; line-height: 1.8; }
.analysis-scene { position: relative; flex: 0 0 290px; width: 100%; max-width: 560px; margin-top: 24px; opacity: .95; }
.scene-grid { position: absolute; bottom: 15px; width: 120px; height: 180px; border: 1px solid rgba(107, 157, 231, .2); transform: skewY(-28deg); background: rgba(124, 172, 239, .04); }
.scene-grid--left { left: 3%; }
.scene-grid--right { right: 8%; height: 125px; }
.scene-window { position: absolute; display: grid; gap: 10px; padding: 19px 20px; border: 1px solid rgba(70, 137, 231, .32); border-radius: 8px; background: rgba(255, 255, 255, .88); box-shadow: 0 14px 25px rgba(75, 124, 203, .14); transform: skewY(-7deg); }
.scene-window--main { top: 26px; left: 21%; width: 65%; height: 166px; }
.scene-window--small { top: 131px; right: 0; width: 38%; height: 112px; padding: 13px 15px; }
.window-dots { display: flex; gap: 4px; }
.window-dots span { width: 6px; height: 6px; border-radius: 50%; background: #6da8ff; }
.window-dots span:nth-child(2) { background: #83d5bd; }
.window-dots span:nth-child(3) { background: #f6c463; }
.window-line { height: 8px; border-radius: 4px; background: #dbe9fb; }
.window-line--long { width: 86%; margin-top: 5px; background: #8bc2fb; }
.window-line--medium { width: 66%; }
.window-line--short { width: 42%; }
.scene-chart { position: absolute; top: 76px; left: 34%; width: 150px; height: 92px; border-bottom: 1px solid #b1cdf0; border-left: 1px solid #b1cdf0; transform: skewY(-7deg); }
.chart-bar { position: absolute; bottom: 0; width: 24px; border-radius: 5px 5px 0 0; background: #69adfa; }
.chart-bar--one { left: 12px; height: 34px; }
.chart-bar--two { left: 52px; height: 60px; background: #74d5bd; }
.chart-bar--three { left: 92px; height: 48px; background: #f6c463; }
.chart-line { position: absolute; top: 36px; left: 14px; width: 125px; height: 3px; border-radius: 2px; background: #ef7f9d; transform: skewY(-20deg) rotate(-12deg); }
.chart-dot { position: absolute; width: 8px; height: 8px; border-radius: 50%; background: #ef7f9d; }
.chart-dot--one { top: 57px; left: 22px; }
.chart-dot--two { top: 27px; left: 72px; }
.chart-dot--three { top: 0; left: 120px; }
.scene-stack { position: absolute; bottom: 13px; left: 24%; display: flex; align-items: end; gap: 8px; transform: skewY(-28deg); }
.scene-stack b { display: block; width: 36px; height: 54px; border-radius: 4px 4px 0 0; background: #6e9fea; box-shadow: inset 0 0 0 1px rgba(255, 255, 255, .42); }
.scene-stack b:nth-child(2) { height: 76px; background: #74d7bf; }
.scene-stack b:nth-child(3) { height: 45px; background: #facb63; }
.scene-node { position: absolute; display: grid; place-items: center; width: 34px; height: 34px; border: 3px solid #fff; border-radius: 50%; color: #fff; font-size: 12px; font-weight: 760; box-shadow: 0 8px 14px rgba(65, 108, 175, .22); }
.scene-node--one { top: 7px; left: 11%; background: #6b9dec; }
.scene-node--two { right: 2%; bottom: 16px; background: #f294a9; }
.scene-node--three { bottom: 3px; left: 49%; background: #6dcdb1; }
.visual-footer { display: flex; justify-content: space-between; gap: 16px; margin-top: auto; padding-top: 22px; color: #7190b7; font-size: 11px; }
.auth-panel { display: flex; flex-direction: column; min-width: 0; padding: 48px 64px; background: #fff; }
.panel-header { text-align: right; color: #6f8097; font-size: 13px; font-weight: 650; }
.auth-heading { margin-top: 104px; }
.auth-heading h2 { margin: 0; color: #172338; font-size: 32px; font-weight: 780; letter-spacing: 0; line-height: 1.3; }
.auth-heading p { margin: 12px 0 0; color: #728198; font-size: 13px; line-height: 1.6; }
.auth-form { margin-top: 24px; }
.auth-form :deep(.el-form-item) { margin-bottom: 22px; }
.auth-form :deep(.el-form-item__label) { padding-bottom: 7px; color: #4b5d76; font-size: 12px; font-weight: 680; }
.auth-form :deep(.el-input__wrapper) { min-height: 43px; border-radius: 7px; background: #fbfdff; }
.auth-form :deep(.el-input__inner) { color: #243750; }
.auth-form :deep(.el-input__inner::placeholder) { color: #a3afbd; }
.auth-form :deep(.el-input__prefix) { color: #6b91bf; font-size: 16px; }
.auth-error { margin-bottom: 16px; overflow-wrap: anywhere; }
.auth-submit { width: 100%; min-height: 43px; margin-top: 7px; border: 0; border-radius: 7px; box-shadow: 0 8px 18px rgba(49, 123, 243, .2); }
.auth-note { margin: auto 0 0; padding-top: 28px; color: #728198; font-size: 11px; line-height: 1.6; text-align: center; }
@media (max-width: 1100px) {
  .auth-panel { padding: 40px 32px; }
  .auth-visual { padding: 34px 30px 28px; }
  .visual-copy h1 { font-size: 42px; }
  .auth-heading h2 { font-size: 28px; }
}
@media (max-width: 900px) {
  .auth-shell { grid-template-columns: 1fr; min-height: auto; }
  .auth-visual { padding: 28px 32px; }
  .visual-copy { margin-top: 32px; }
  .analysis-scene { display: none; }
  .visual-footer { padding-top: 24px; }
  .auth-panel { min-height: 490px; padding: 32px; }
  .auth-heading { margin-top: 32px; }
}
@media (max-width: 560px) {
  .auth-page { padding: 0; place-items: start center; }
  .auth-shell { border: 0; border-radius: 0; min-height: 100dvh; }
  .auth-visual { padding: 24px 22px; }
  .visual-copy { margin-top: 24px; }
  .visual-copy h1 { font-size: 34px; }
  .visual-copy p { margin-top: 12px; font-size: 12px; }
  .visual-footer { display: none; }
  .auth-panel { padding: 26px 22px 24px; min-height: 460px; }
  .auth-heading { margin-top: 24px; }
  .auth-heading h2 { font-size: 27px; }
}
</style>
