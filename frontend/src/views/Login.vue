<template>
  <div class="login-container">
    <div class="login-orb login-orb-1"></div>
    <div class="login-orb login-orb-2"></div>
    <div class="login-orb login-orb-3"></div>
    <div class="login-card">
      <h2 class="login-title">Gitea Manager</h2>
      <p class="login-subtitle">安全登录到管理控制台</p>
      <el-form @submit.prevent="doLogin" class="login-form">
        <el-form-item label="登录方式">
          <el-select v-model="source" aria-label="登录方式" class="login-source"><el-option label="本地应急管理员" value="local" /><el-option v-for="item in sources.filter(s => s.source === 'ldap' || s.type === 'ldap' || s.id === 'ldap')" :key="item.id || item.source" :label="item.name || '公司 LDAP / AD'" value="ldap" /></el-select>
        </el-form-item>
        <el-form-item label="账号">
          <el-input v-model="username" autocomplete="username" :placeholder="source === 'local' ? '本地管理员账号' : '公司目录账号'" aria-label="登录账号" size="large" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="password" type="password" show-password autocomplete="current-password" placeholder="请输入密码" aria-label="登录密码" size="large" prefix-icon="Lock" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" native-type="submit" :loading="loading" class="login-btn">登 录</el-button>
        </el-form-item>
      </el-form>
      <p v-if="error" class="login-error">{{ error }}</p>
      <p class="login-footer">Powered by Gitea Manager v2.0</p>
    </div>
  </div>
</template>

<script>
import { ref, watch } from 'vue'
import { api } from '../api'

export default {
  props: { sources: { type: Array, default: () => [] } },
  emits: ['login'],
  setup(props, { emit }) {
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

    return { source, username, password, loading, error, doLogin }
  },
}
</script>

<style scoped>
.login-container {
  display: flex; justify-content: center; align-items: center; height: 100vh;
  background: var(--gradient-bg);
  position: relative; overflow: hidden;
}
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

.login-source { width: 100%; }
.login-card {
  box-sizing: border-box; max-width: calc(100vw - 32px); width: 460px; padding: 48px 40px; border-radius: 28px;
  background: var(--glass-dialog); backdrop-filter: var(--blur-dialog); -webkit-backdrop-filter: var(--blur-dialog);
  border: 1px solid rgba(255,255,255,0.68); box-shadow: inset 0 1px 0 rgba(255,255,255,0.88), var(--shadow-lg);
  animation: fadeInUp 0.24s ease; position: relative; z-index: 1;
}
.login-title {
  text-align: center; font-size: 26px; font-weight: 760; margin-bottom: 8px;
  color: var(--text-primary);
}
.login-subtitle { text-align: center; color: var(--text-secondary); font-size: 14px; margin-bottom: 32px; }
.login-form :deep(.el-input__wrapper) {
  background: rgba(255,255,255,0.66) !important;
  border-radius: 16px !important; padding: 4px 12px !important;
}
.login-btn {
  width: 100%; height: 44px !important; font-size: 15px !important; font-weight: 600 !important;
  border-radius: 16px !important;
}
.login-error { color: var(--color-danger); text-align: center; margin-top: 8px; font-size: 13px; }
.login-footer { text-align: center; margin-top: 20px; font-size: 12px; color: var(--text-muted); }
</style>
