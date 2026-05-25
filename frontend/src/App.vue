<template>
  <div v-if="!authenticated">
    <LoginView @login="onLogin" />
  </div>
  <div v-else class="layout">
    <el-container>
      <el-aside :width="collapsed ? '64px' : '220px'" style="transition:width .3s">
        <div class="logo">{{ collapsed ? 'G' : 'Gitea Manager' }}</div>
        <div class="collapse-btn" @click="collapsed = !collapsed">
          {{ collapsed ? '☰' : '✕' }}
        </div>
        <el-menu
          :default-active="activeMenu"
          :collapse="collapsed"
          router
          background-color="#1a1d2e"
          text-color="#8a8fbf"
          active-text-color="#409eff"
        >
          <el-menu-item index="/dashboard">
            <el-icon><Monitor /></el-icon>
            <template #title>仪表盘</template>
          </el-menu-item>
          <el-menu-item index="/servers">
            <el-icon><Setting /></el-icon>
            <template #title>服务器管理</template>
          </el-menu-item>
          <el-menu-item index="/backups">
            <el-icon><FolderOpened /></el-icon>
            <template #title>备份管理</template>
          </el-menu-item>
          <el-menu-item index="/restore">
            <el-icon><RefreshRight /></el-icon>
            <template #title>恢复操作</template>
          </el-menu-item>
          <el-menu-item index="/settings">
            <el-icon><Tools /></el-icon>
            <template #title>系统设置</template>
          </el-menu-item>
          <el-menu-item index="/schedules">
            <el-icon><Clock /></el-icon>
            <template #title>定时任务</template>
          </el-menu-item>
        </el-menu>
      </el-aside>
      <el-container>
        <el-header class="header">
          <span class="header-title">⚙ Gitea 管理系统</span>
          <el-button type="danger" size="small" @click="logout">退出登录</el-button>
        </el-header>
        <el-main>
          <div v-if="needsConfig" class="blocking-card">
            <el-result icon="warning" title="请先配置本机 IP" sub-title="使用平台前需在系统设置中配置本机 IP，用于区分本地和远程服务器。">
              <template #extra>
                <el-button type="primary" @click="$router.push('/settings')">前往系统设置</el-button>
              </template>
            </el-result>
          </div>
          <router-view v-else />
        </el-main>
      </el-container>
    </el-container>
  </div>
</template>

<script>
import { ref, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import LoginView from './views/Login.vue'
import { Monitor, Setting, FolderOpened, RefreshRight, Tools, Clock } from '@element-plus/icons-vue'
import { api } from './api'

export default {
  components: { LoginView, Monitor, Setting, FolderOpened, RefreshRight, Tools, Clock },
  setup() {
    const router = useRouter()
    const route = useRoute()
    const authenticated = ref(false)
    const needsConfig = ref(false)
    const collapsed = ref(false)

    const activeMenu = computed(() => route.path || '/dashboard')

    function checkSettings() {
      api.get('/settings').then(res => {
        needsConfig.value = !res.data.host_ip
      }).catch(() => { needsConfig.value = false })
    }

    api.get('/session').then(res => {
      authenticated.value = res.data.authenticated
      if (res.data.authenticated) checkSettings()
    }).catch(() => {
      authenticated.value = false
    })

    function onLogin() {
      authenticated.value = true
      router.push('/dashboard')
    }

    function logout() {
      api.post('/logout').then(() => {
        authenticated.value = false
        router.push('/')
      })
    }

    return { authenticated, needsConfig, collapsed, activeMenu, onLogin, logout }
  },
}
</script>

<style>
body { margin: 0; font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; }
.layout { height: 100vh; }
.el-aside { background-color: #1a1d2e; overflow: hidden; }
.logo { color: #fff; text-align: center; line-height: 60px; font-size: 20px; font-weight: bold; border-bottom: 1px solid rgba(255,255,255,0.08); letter-spacing: 1px; }
.collapse-btn { color: #8a8fbf; text-align: center; line-height: 36px; cursor: pointer; font-size: 16px; border-bottom: 1px solid rgba(255,255,255,0.08); user-select: none; transition: all .2s; }
.collapse-btn:hover { color: #409eff; background: rgba(64,158,255,0.1); }
.header { background: #fff; border-bottom: 1px solid #ebeef5; display: flex; align-items: center; justify-content: space-between; padding: 0 20px; box-shadow: 0 1px 4px rgba(0,0,0,0.04); }
.header-title { font-size: 15px; font-weight: 600; color: #303133; }
.el-main { background: #f0f2f5; min-height: calc(100vh - 60px); padding: 20px; }
.blocking-card { display: flex; justify-content: center; align-items: center; height: calc(100vh - 120px); }

.el-menu { border-right: none; }
.el-menu-item { transition: all .2s; }
.el-menu-item.is-active { border-left: 3px solid #409eff; background: linear-gradient(90deg, rgba(64,158,255,0.08), transparent) !important; }
.el-menu-item:hover { background: rgba(255,255,255,0.04) !important; }
</style>
