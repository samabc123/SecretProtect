<template>
  <main class="auth-page">
    <div class="auth-shell">
      <section class="auth-visual" aria-label="平台能力展示">
        <div class="visual-topline">
          <div class="visual-brand">
            <SecretRadarLogo :size="40" :animated="true" variant="dark" />
            <span>云上凭据泄露自动化检测</span>
          </div>
        </div>

        <div class="visual-copy">
          <h1>让每一条证据<br>可追溯</h1>
          <p>
            统一连接采集渠道、检测规则、凭据实体与验证结果，
            在授权范围内完成可审计的安全运营。
          </p>
        </div>

        <div class="analysis-scene" aria-hidden="true">
          <div class="scene-grid scene-grid--left" />
          <div class="scene-grid scene-grid--right" />
          <div class="scene-chart">
            <span class="chart-bar chart-bar--one" />
            <span class="chart-bar chart-bar--two" />
            <span class="chart-bar chart-bar--three" />
            <span class="chart-line" />
            <i class="chart-dot chart-dot--one" />
            <i class="chart-dot chart-dot--two" />
            <i class="chart-dot chart-dot--three" />
          </div>
          <div class="scene-window scene-window--main">
            <span /><span /><span />
            <div class="window-line window-line--long" />
            <div class="window-line window-line--medium" />
            <div class="window-line window-line--short" />
          </div>
          <div class="scene-window scene-window--small">
            <span /><span /><span />
            <div class="window-line window-line--medium" />
            <div class="window-line window-line--short" />
          </div>
          <div class="scene-stack">
            <b /><b /><b />
          </div>
          <div class="scene-node scene-node--one">规</div>
          <div class="scene-node scene-node--two">证</div>
          <div class="scene-node scene-node--three">验</div>
          <div class="scene-orbit" />
        </div>

        <div class="visual-footer">
          <span>敏感值默认脱敏</span>
          <span>授权范围内运行</span>
          <span>证据链可回溯</span>
        </div>
      </section>

      <section class="auth-panel" aria-labelledby="auth-title">
        <div class="panel-header">
          <span class="panel-mode">你好，欢迎回来！</span>
        </div>

        <div class="auth-heading">
          <h2 id="auth-title">Samlab 安全管理</h2>
          <p>管理员登录</p>
        </div>

        <el-form ref="formRef" class="auth-form" :model="form" :rules="rules" label-position="top" @submit.prevent="submit">
          <el-form-item label="用户名" prop="username">
            <el-input id="username" v-model="form.username" autocomplete="username" placeholder="请输入用户名">
              <template #prefix>
                <el-icon class="input-prefix"><User /></el-icon>
              </template>
            </el-input>
          </el-form-item>
          <el-form-item label="密码" prop="password">
            <el-input
              id="password"
              v-model="form.password"
              type="password"
              show-password
              autocomplete="current-password"
              placeholder="请输入密码"
              @keyup.enter="submit"
            >
              <template #prefix>
                <el-icon class="input-prefix"><Lock /></el-icon>
              </template>
            </el-input>
          </el-form-item>
          <el-button type="primary" native-type="submit" :loading="loading" class="auth-submit">
            登录
          </el-button>
        </el-form>

        <p class="auth-note">敏感信息默认脱敏 · 所有操作保留审计证据</p>
      </section>
    </div>
  </main>
</template>

<script setup>
import { reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { Lock, User } from "@element-plus/icons-vue";
import { useUserStore } from "../store/modules/user";
import SecretRadarLogo from "../components/SecretRadarLogo.vue";

const route = useRoute();
const router = useRouter();
const userStore = useUserStore();
const loading = ref(false);
const formRef = ref();
const form = reactive({ username: "", password: "" });
const rules = {
  username: [{ required: true, message: "请输入用户名", trigger: "blur" }],
  password: [{ required: true, min: 6, message: "密码至少 6 位", trigger: "blur" }],
};

async function submit() {
  await formRef.value.validate();
  loading.value = true;
  try {
    await userStore.login({ username: form.username, password: form.password });
    router.replace(userStore.user?.needs_password_change ? "/settings#account" : String(route.query.next || "/admission"));
  } catch (error) {
    ElMessage.error(error.response?.data?.detail || "操作失败，请检查输入");
  } finally {
    loading.value = false;
  }
}
</script>

<style scoped>
.auth-page {
  --auth-ink: #172338;
  --auth-muted: #728198;
  --auth-blue: #317bf3;
  min-height: 100vh;
  padding: 28px;
  overflow: hidden;
  background: #eaf2ff;
}

.auth-page::before,
.auth-page::after {
  display: none;
  content: "";
}

.auth-shell {
  display: grid;
  grid-template-columns: minmax(0, 1.08fr) minmax(390px, .92fr);
  width: min(1240px, 100%);
  min-height: min(760px, calc(100vh - 56px));
  margin: 0 auto;
  overflow: hidden;
  border: 1px solid rgba(173, 199, 228, .78);
  border-radius: 18px;
  background: #fff;
  box-shadow: 0 24px 60px rgba(57, 93, 146, .16);
}

.auth-visual {
  position: relative;
  display: flex;
  flex-direction: column;
  min-width: 0;
  padding: 34px 42px 28px;
  overflow: hidden;
  background: #eaf2ff;
}

.visual-topline,
.panel-header,
.visual-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.visual-brand {
  display: inline-flex;
  align-items: center;
  gap: 10px;
}

.visual-brand > span:last-child {
  color: #23416a;
  font-size: 17px;
  font-weight: 760;
  line-height: 1.35;
}

.visual-copy {
  position: relative;
  z-index: 1;
  max-width: 520px;
  margin-top: clamp(68px, 12vh, 118px);
}

.auth-kicker {
  margin: 0 0 12px;
  color: #4e87da;
  font-size: 11px;
  font-weight: 760;
  letter-spacing: .14em;
}

.visual-copy h1 {
  max-width: 520px;
  margin: 0;
  color: #1e3556;
  font-size: clamp(34px, 4vw, 56px);
  font-weight: 780;
  letter-spacing: -.035em;
  line-height: 1.08;
}

.visual-copy p:last-child {
  max-width: 480px;
  margin: 18px 0 0;
  color: #6c83a2;
  font-size: 14px;
  line-height: 1.8;
}

.analysis-scene {
  position: absolute;
  right: 3%;
  bottom: 8%;
  width: min(47vw, 560px);
  height: 340px;
  opacity: .95;
}

.scene-grid {
  position: absolute;
  bottom: 15px;
  width: 120px;
  height: 180px;
  border: 1px solid rgba(107, 157, 231, .2);
  transform: skewY(-28deg);
  background: rgba(124, 172, 239, .04);
}

.scene-grid--left { left: 3%; }
.scene-grid--right { right: 8%; height: 125px; }

.scene-window {
  position: absolute;
  display: grid;
  gap: 10px;
  padding: 19px 20px;
  border: 1px solid rgba(70, 137, 231, .32);
  border-radius: 10px;
  background: rgba(255, 255, 255, .88);
  box-shadow: 0 14px 25px rgba(75, 124, 203, .14);
  transform: skewY(-7deg);
}

.scene-window--main { top: 26px; left: 21%; width: 286px; height: 166px; }
.scene-window--small { top: 131px; right: 6%; width: 178px; height: 112px; padding: 13px 15px; }
.scene-window > span { display: inline-block; width: 6px; height: 6px; margin-right: 4px; border-radius: 50%; background: #6da8ff; }
.scene-window > span:nth-child(2) { background: #83d5bd; }
.scene-window > span:nth-child(3) { background: #f6c463; }
.window-line { height: 8px; border-radius: 4px; background: #dbe9fb; }
.window-line--long { width: 86%; margin-top: 5px; background: #8bc2fb; }
.window-line--medium { width: 66%; }
.window-line--short { width: 42%; }

.scene-chart {
  position: absolute;
  top: 76px;
  left: 34%;
  width: 194px;
  height: 92px;
  border-bottom: 1px solid #b1cdf0;
  border-left: 1px solid #b1cdf0;
  transform: skewY(-7deg);
}

.chart-bar { position: absolute; bottom: 0; width: 24px; border-radius: 5px 5px 0 0; background: #69adfa; }
.chart-bar--one { left: 22px; height: 34px; }
.chart-bar--two { left: 62px; height: 60px; background: #74d5bd; }
.chart-bar--three { left: 102px; height: 48px; background: #f6c463; }
.chart-line { position: absolute; top: 46px; left: 24px; width: 150px; height: 3px; border-radius: 2px; background: #ef7f9d; transform: skewY(-20deg) rotate(-12deg); }
.chart-dot { position: absolute; width: 8px; height: 8px; border-radius: 50%; background: #ef7f9d; }
.chart-dot--one { top: 31px; left: 47px; }
.chart-dot--two { top: 16px; left: 104px; }
.chart-dot--three { top: 3px; left: 156px; }

.scene-stack {
  position: absolute;
  bottom: 13px;
  left: 24%;
  display: flex;
  align-items: end;
  gap: 8px;
  transform: skewY(-28deg);
}

.scene-stack b { display: block; width: 36px; height: 54px; border-radius: 4px 4px 0 0; background: #6e9fea; box-shadow: inset 0 0 0 1px rgba(255, 255, 255, .42); }
.scene-stack b:nth-child(2) { height: 76px; background: #74d7bf; }
.scene-stack b:nth-child(3) { height: 45px; background: #facb63; }

.scene-node {
  position: absolute;
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border: 3px solid #fff;
  border-radius: 50%;
  color: #fff;
  font-size: 12px;
  font-weight: 760;
  box-shadow: 0 8px 14px rgba(65, 108, 175, .22);
}

.scene-node--one { top: 7px; left: 11%; background: #6b9dec; }
.scene-node--two { right: 2%; bottom: 54px; background: #f294a9; }
.scene-node--three { bottom: 3px; left: 49%; background: #6dcdb1; }
.scene-orbit { position: absolute; right: 11%; bottom: 2%; width: 70px; height: 32px; border: 3px solid #6ba8f5; border-radius: 50%; transform: rotate(-10deg); }

.visual-footer {
  position: relative;
  z-index: 1;
  margin-top: auto;
  padding-top: 22px;
  color: #7190b7;
  font-size: 11px;
}

.auth-panel {
  display: flex;
  flex-direction: column;
  min-width: 0;
  padding: 48px clamp(32px, 5vw, 72px);
  background: #fff;
}

.panel-mode {
  margin-left: auto;
  color: #6f8097;
  font-size: 13px;
  font-weight: 650;
}

.auth-heading {
  margin-top: clamp(62px, 10vh, 112px);
}

.auth-heading h2 {
  margin: 0;
  color: var(--auth-ink);
  font-size: clamp(25px, 2.5vw, 34px);
  font-weight: 780;
  letter-spacing: -.025em;
  line-height: 1.2;
}

.auth-heading p:last-child {
  margin: 12px 0 0;
  color: var(--auth-muted);
  font-size: 13px;
  line-height: 1.6;
}

.auth-tabs {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 4px;
  margin-top: 30px;
  padding: 4px;
  border: 1px solid #e4ebf3;
  border-radius: 8px;
  background: #f6f8fb;
}

.auth-tabs button {
  min-height: 36px;
  border: 0;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 650;
}

.auth-tabs button.is-active {
  background: #fff;
  color: #2468c0;
  box-shadow: 0 3px 8px rgba(37, 83, 135, .1);
}

.auth-form { margin-top: 24px; }
.auth-form :deep(.el-form-item) { margin-bottom: 19px; }
.auth-form :deep(.el-form-item__label) { padding-bottom: 7px; color: #4b5d76; font-size: 12px; font-weight: 680; }
.auth-form :deep(.el-input__wrapper) { min-height: 43px; border-radius: 7px; background: #fbfdff; }
.auth-form :deep(.el-input__inner) { color: #243750; }
.auth-form :deep(.el-input__inner::placeholder) { color: #a3afbd; }
.input-prefix { color: #6b91bf; font-size: 16px; }
.auth-submit { width: 100%; min-height: 43px; margin-top: 7px; border: 0; border-radius: 7px; box-shadow: 0 8px 18px rgba(49, 123, 243, .2); }

.auth-note { margin: auto 0 0; padding-top: 28px; color: #9ba7b6; font-size: 11px; line-height: 1.6; text-align: center; }

@media (max-width: 900px) {
  .auth-shell { grid-template-columns: 1fr; min-height: auto; }
  .auth-visual { min-height: 390px; }
  .auth-panel { min-height: 650px; }
  .visual-copy { margin-top: 55px; }
  .auth-heading { margin-top: 58px; }
}

@media (max-width: 560px) {
  .auth-page { padding: 0; }
  .auth-shell { border: 0; border-radius: 0; }
  .auth-visual { min-height: 340px; padding: 24px 22px; }
  .auth-panel { padding: 30px 22px 24px; }
  .visual-copy { margin-top: 48px; }
  .visual-copy h1 { max-width: 310px; font-size: 34px; }
  .visual-copy p:last-child { max-width: 300px; font-size: 12px; }
  .visual-footer { gap: 8px; font-size: 10px; }
  .visual-footer span:nth-child(2) { display: none; }
  .analysis-scene { right: -100px; bottom: -90px; width: 420px; height: 275px; opacity: .62; }
  .auth-heading { margin-top: 45px; }
  .auth-heading h2 { font-size: 27px; }
  .auth-note { padding-top: 24px; }
}

@media (prefers-reduced-motion: reduce) {
  .auth-page *,
  .auth-page *::before,
  .auth-page *::after {
    transition-duration: .01ms !important;
    animation-duration: .01ms !important;
  }
}
</style>
