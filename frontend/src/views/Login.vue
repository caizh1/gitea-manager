<template>
  <div class="login-container">
    <el-card class="login-card" shadow="always">
      <template #header>
        <h2 style="text-align:center;margin:0">Gitea Manager</h2>
      </template>
      <el-form @submit.prevent="doLogin">
        <el-form-item label="管理员密码">
          <el-input v-model="password" type="password" show-password placeholder="请输入密码" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="doLogin" :loading="loading" style="width:100%">登录</el-button>
        </el-form-item>
      </el-form>
      <p v-if="error" style="color:red;text-align:center">{{ error }}</p>
    </el-card>
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
.login-container { display: flex; justify-content: center; align-items: center; height: 100vh; background: #2d3a4b; }
.login-card { width: 400px; }
</style>
