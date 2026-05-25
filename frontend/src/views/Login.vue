<template>
  <div class="login-container">
    <div class="login-orb login-orb-1"></div>
    <div class="login-orb login-orb-2"></div>
    <div class="login-orb login-orb-3"></div>
    <div class="login-card">
      <h2 class="login-title">Gitea Manager</h2>
      <p class="login-subtitle">安全登录到管理控制台</p>
      <el-form @submit.prevent="doLogin" class="login-form">
        <el-form-item>
          <el-input v-model="password" type="password" show-password placeholder="请输入管理员密码" size="large" prefix-icon="Lock" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="doLogin" :loading="loading" class="login-btn">登 录</el-button>
        </el-form-item>
      </el-form>
      <p v-if="error" class="login-error">{{ error }}</p>
      <p class="login-footer">Powered by Gitea Manager v2.0</p>
    </div>
  </div>
</template>

<script>
import { ref } from 'vue'
import { api } from '../api'

export default {
  emits: ['login'],
  setup(props, { emit }) {
    const password = ref('')
    const loading = ref(false)
    const error = ref('')

    function doLogin() {
      error.value = ''
      loading.value = true
      api.post('/login', { password: password.value }).then(() => {
        emit('login')
      }).catch(() => {
        error.value = '密码错误'
      }).finally(() => {
        loading.value = false
      })
    }

    return { password, loading, error, doLogin }
  },
}
</script>

<style scoped>
.login-container {
  display: flex; justify-content: center; align-items: center; height: 100vh;
  background: linear-gradient(-45deg, #667eea, #764ba2, #f093fb, #f5576c, #4facfe, #00f2fe);
  background-size: 400% 400%; animation: gradientFlow 12s ease infinite;
  position: relative; overflow: hidden;
}
.login-container::before {
  content: ''; position: absolute; top: -50%; left: -50%; width: 200%; height: 200%;
  background: radial-gradient(circle at 30% 40%, rgba(255,255,255,0.15) 0%, transparent 50%),
              radial-gradient(circle at 70% 60%, rgba(255,255,255,0.1) 0%, transparent 40%);
  pointer-events: none;
}
.login-orb {
  position: absolute; border-radius: 50%; filter: blur(80px); opacity: 0.4;
  pointer-events: none;
}
.login-orb-1 { width: 500px; height: 500px; top: -10%; left: -5%; background: #667eea; animation: float1 8s ease-in-out infinite; }
.login-orb-2 { width: 400px; height: 400px; bottom: -10%; right: -5%; background: #f093fb; animation: float2 6s ease-in-out infinite 1s; }
.login-orb-3 { width: 300px; height: 300px; top: 40%; left: 60%; background: #4facfe; animation: float3 10s ease-in-out infinite 2s; }
@keyframes float1 { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-30px); } }
@keyframes float2 { 0%,100% { transform: translateY(0); } 50% { transform: translateY(20px); } }
@keyframes float3 { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-15px); } }

.login-card {
  width: 420px; padding: 48px 40px; border-radius: 20px;
  background: rgba(255,255,255,0.72); backdrop-filter: blur(40px); -webkit-backdrop-filter: blur(40px);
  border: 1px solid rgba(255,255,255,0.35); box-shadow: 0 16px 48px rgba(0,0,0,0.12), inset 0 1px 0 rgba(255,255,255,0.5);
  animation: fadeInUp 0.6s ease; position: relative; z-index: 1;
}
.login-title {
  text-align: center; font-size: 26px; font-weight: 700; margin-bottom: 8px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  background-clip: text;
}
.login-subtitle { text-align: center; color: #6b7280; font-size: 14px; margin-bottom: 32px; }
.login-form :deep(.el-input__wrapper) {
  background: rgba(255,255,255,0.6) !important;
  border-radius: 10px !important; padding: 4px 12px !important;
}
.login-btn {
  width: 100%; height: 44px !important; font-size: 15px !important; font-weight: 600 !important;
  border-radius: 10px !important;
}
.login-error { color: #ef4444; text-align: center; margin-top: 8px; font-size: 13px; }
.login-footer { text-align: center; margin-top: 20px; font-size: 12px; color: #9ca3af; }
</style>
