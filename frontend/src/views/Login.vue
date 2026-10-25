<template>
  <main class="login-container">
    <div class="login-background" aria-hidden="true">
      <div class="login-orb login-orb-1"></div>
      <div class="login-orb login-orb-2"></div>
      <div class="login-orb login-orb-3"></div>
    </div>
    <section class="login-card" aria-labelledby="login-title">
      <h1 id="login-title" class="login-title">Gitea Manager</h1>
      <p class="login-subtitle">安全登录到管理控制台</p>

      <div class="login-access">
        <div v-if="directorySource" class="login-methods" role="group" aria-label="登录方式">
          <button type="button" class="login-method" :aria-pressed="source === 'local'" :disabled="loading" @click="source = 'local'">
            <el-icon aria-hidden="true"><UserFilled /></el-icon>
            <span>本地管理员</span>
          </button>
          <button type="button" class="login-method" :aria-pressed="source === 'ldap'" :disabled="loading" @click="source = 'ldap'">
            <el-icon aria-hidden="true"><OfficeBuilding /></el-icon>
            <span>{{ directorySource.name || '公司 LDAP / AD' }}</span>
          </button>
        </div>
        <div v-else class="login-identity">
          <el-icon aria-hidden="true"><UserFilled /></el-icon>
          <span>本地管理员登录</span>
        </div>
        <p id="login-method-hint" class="login-method-hint">{{ source === 'local' ? '使用本地账号进行平台管理与应急维护' : '使用公司 LDAP / AD 目录账号登录' }}</p>
      </div>

      <el-form @submit.prevent="doLogin" class="login-form" label-position="top" aria-describedby="login-method-hint">
        <el-form-item label="账号" for="login-username">
          <el-input id="login-username" v-model="username" autocomplete="username" :placeholder="source === 'local' ? '请输入本地管理员账号' : '请输入公司目录账号'" :disabled="loading" size="large" prefix-icon="User" />
        </el-form-item>
        <el-form-item label="密码" for="login-password">
          <el-input id="login-password" v-model="password" type="password" show-password autocomplete="current-password" placeholder="请输入密码" :disabled="loading" size="large" prefix-icon="Lock" />
        </el-form-item>
        <p v-if="error" class="login-error" role="alert">{{ error }}</p>
        <el-button type="primary" native-type="submit" :loading="loading" class="login-btn">登录</el-button>
      </el-form>
      <p class="login-footer">Powered by Gitea Manager v2.0</p>
    </section>
  </main>
</template>

<script>
import { computed, ref, watch } from 'vue'
import { api } from '../api'

export default {
  props: { sources: { type: Array, default: () => [] } },
  emits: ['login'],
  setup(props, { emit }) {
    const directorySource = computed(() => props.sources.find(s => s.source === 'ldap' || s.type === 'ldap' || s.id === 'ldap'))
    const source = ref('local'), username = ref('admin')
    watch(source, value => { username.value = value === 'local' ? 'admin' : ''; password.value = ''; error.value = '' })
    const password = ref('')
    const loading = ref(false)
    const error = ref('')

    function doLogin() {
      if (loading.value) return
      error.value = ''
      loading.value = true
      api.post('/login', { source: source.value, username: username.value, password: password.value }).then(() => {
        emit('login')
      }).catch(e => {
        error.value = e.response?.data?.error || '登录失败，请检查连接和账号密码'
      }).finally(() => {
        loading.value = false
      })
    }

    return { source, username, password, loading, error, directorySource, doLogin }
  },
}
</script>

<style scoped>
.login-container {
  display: flex; justify-content: center; align-items: center;
  min-height: 100vh; min-height: 100dvh; padding: 32px 20px;
  background: var(--gradient-bg);
  position: relative;
}
.login-background { position: absolute; inset: 0; overflow: hidden; pointer-events: none; }
.login-container::before {
  content: ''; position: absolute; inset: 0;
  background:
    radial-gradient(circle at 28% 32%, rgba(255,255,255,0.42) 0, rgba(255,255,255,0.18) 22%, transparent 42%),
    radial-gradient(circle at 70% 66%, rgba(255,255,255,0.34) 0, rgba(255,255,255,0.14) 20%, transparent 38%);
  pointer-events: none;
}
.login-orb {
  position: absolute; border-radius: 46% 54% 50% 50%; opacity: 0.78;
  pointer-events: none;
  background: rgba(255,255,255,0.26);
  box-shadow: inset 18px 22px 52px rgba(255,255,255,0.56), inset -16px -18px 44px rgba(148,163,184,0.10);
}
.login-orb-1 { width: 460px; height: 460px; top: -10%; left: -4%; animation: float1 12s ease-in-out infinite; }
.login-orb-2 { width: 360px; height: 360px; bottom: -10%; right: -3%; animation: float2 14s ease-in-out infinite 1s; }
.login-orb-3 { width: 240px; height: 240px; top: 48%; left: 62%; animation: float3 16s ease-in-out infinite 2s; }
@keyframes float1 { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-30px); } }
@keyframes float2 { 0%,100% { transform: translateY(0); } 50% { transform: translateY(20px); } }
@keyframes float3 { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-15px); } }

.login-card {
  box-sizing: border-box; max-width: 100%; min-width: 0; width: 440px; padding: 40px; border-radius: 28px;
  background: var(--glass-dialog); backdrop-filter: var(--blur-dialog); -webkit-backdrop-filter: var(--blur-dialog);
  border: 1px solid rgba(255,255,255,0.68); box-shadow: inset 0 1px 0 rgba(255,255,255,0.88), var(--shadow-lg);
  animation: fadeInUp 0.24s ease; position: relative; z-index: 1;
}
.login-title {
  text-align: center; font-size: 26px; font-weight: 760; margin-bottom: 8px;
  color: var(--text-primary);
}
.login-subtitle { text-align: center; color: var(--text-secondary); font-size: 14px; margin-bottom: 28px; }
.login-access { margin-bottom: 24px; padding-bottom: 22px; border-bottom: 1px solid var(--glass-border-dark); }
.login-methods {
  display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 4px;
  padding: 4px; border-radius: 14px; background: rgba(15, 23, 42, 0.045);
}
.login-method {
  display: flex; align-items: center; justify-content: center; gap: 8px; min-width: 0;
  padding: 11px 10px; border: 1px solid transparent; border-radius: 11px;
  background: transparent; color: var(--text-secondary); font: inherit; font-size: 13px;
  line-height: 20px; cursor: pointer; transition: background 0.18s ease, box-shadow 0.18s ease;
}
.login-method span { min-width: 0; overflow-wrap: anywhere; }
.login-method .el-icon, .login-identity .el-icon { flex: 0 0 16px; width: 16px; height: 16px; }
.login-method:hover:not(:disabled) { color: var(--color-primary); background: rgba(255, 255, 255, 0.6); }
.login-method[aria-pressed="true"] {
  background: #fff; color: var(--color-primary); font-weight: 600;
  border-color: rgba(15, 23, 42, 0.05); box-shadow: var(--shadow-xs);
}
.login-method:focus-visible { outline: 2px solid var(--color-primary); outline-offset: 2px; }
.login-method:disabled { cursor: wait; opacity: 0.65; }
.login-identity { display: flex; align-items: center; justify-content: center; gap: 8px; color: var(--color-primary); font-size: 14px; font-weight: 600; }
.login-method-hint { margin-top: 12px; color: var(--text-secondary); font-size: 12px; line-height: 1.6; text-align: center; }
.login-form :deep(.el-form-item) { margin-bottom: 20px; }
.login-form :deep(.el-form-item__label) { height: auto; margin-bottom: 8px; padding: 0; line-height: 20px; color: var(--text-primary); font-size: 13px; font-weight: 600; }
.login-form :deep(.el-input__wrapper) {
  background: rgba(255,255,255,0.66) !important;
  min-height: 46px; gap: 8px; border-radius: 12px !important; padding: 4px 14px !important;
}
.login-form :deep(.el-input__icon) { margin: 0; }
.login-form :deep(.el-input__inner) { min-width: 0; }
.login-btn {
  width: 100%; height: 46px !important; margin-top: 4px; font-size: 15px !important; font-weight: 600 !important;
  border-radius: 12px !important;
}
.login-error { margin-bottom: 16px; padding: 10px 12px; border-radius: 10px; background: var(--color-danger-light); color: var(--color-danger); line-height: 1.6; overflow-wrap: anywhere; font-size: 13px; }
.login-footer { text-align: center; margin-top: 24px; font-size: 12px; color: var(--text-muted); }
@media (max-width: 480px) {
  .login-container { padding: 24px 16px; }
  .login-card { padding: 32px 24px; border-radius: 24px; }
  .login-title { font-size: 24px; }
  .login-method { gap: 6px; padding: 10px 6px; }
}
@media (prefers-reduced-motion: reduce) {
  .login-orb, .login-card { animation: none; }
}
</style>
