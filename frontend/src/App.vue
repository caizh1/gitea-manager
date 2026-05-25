<template>
  <div v-if="!authenticated">
    <LoginView @login="onLogin" />
  </div>
  <div v-else class="layout">
    <el-container>
      <el-aside :width="collapsed ? '64px' : '240px'" class="app-aside">
        <div class="sidebar-logo">
          <h1 v-if="!collapsed" class="logo-text">Gitea Manager</h1>
          <span v-else class="logo-icon">G</span>
          <span v-if="!collapsed" class="logo-sub">Management Console</span>
        </div>
        <div class="collapse-btn" @click="collapsed = !collapsed">
          <el-icon :size="16"><component :is="collapsed ? 'Expand' : 'Fold'" /></el-icon>
        </div>
        <el-menu
          :default-active="activeMenu"
          :collapse="collapsed"
          router
          background-color="transparent"
          text-color="rgba(255,255,255,0.55)"
          active-text-color="#fff"
          class="app-menu"
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
          <el-menu-item index="/mirrors">
            <el-icon><Connection /></el-icon>
            <template #title>镜像管理</template>
          </el-menu-item>
          <el-menu-item index="/statistics">
            <el-icon><DataAnalysis /></el-icon>
            <template #title>统计分析</template>
          </el-menu-item>
        </el-menu>
        <div class="sidebar-footer">
          <div class="sidebar-user">
            <div class="user-avatar">A</div>
            <div v-if="!collapsed" class="user-info">
              <div class="user-name">Admin</div>
              <div class="user-role">超级管理员</div>
            </div>
          </div>
        </div>
      </el-aside>
      <el-container>
        <el-header class="app-header">
          <div class="header-left">
            <div class="breadcrumb">
              <span class="breadcrumb-item">{{ breadcrumbParent }}</span>
              <span v-if="breadcrumbCurrent" class="breadcrumb-sep">/</span>
              <span v-if="breadcrumbCurrent" class="breadcrumb-current">{{ breadcrumbCurrent }}</span>
            </div>
          </div>
          <div class="header-right">
            <AlertBell />
            <el-button size="small" @click="logout" class="logout-btn">
              <el-icon><SwitchButton /></el-icon>
              <span>退出登录</span>
            </el-button>
          </div>
        </el-header>
        <el-main class="app-main">
          <div v-if="needsConfig && route.path !== '/settings'" class="blocking-card">
            <el-result icon="warning" title="请先配置本机 IP" sub-title="使用平台前需在系统设置中配置本机 IP，用于区分本地和远程服务器。">
              <template #extra>
                <el-button type="primary" @click="$router.push('/settings')">前往系统设置</el-button>
              </template>
            </el-result>
          </div>
          <router-view v-else v-slot="{ Component }">
            <transition name="page" mode="out-in">
              <component :is="Component" />
            </transition>
          </router-view>
        </el-main>
      </el-container>
    </el-container>
  </div>
</template>

<script>
import { ref, computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import LoginView from './views/Login.vue'
import AlertBell from './components/AlertBell.vue'
import { Monitor, Setting, FolderOpened, RefreshRight, Tools, Clock, Expand, Fold, SwitchButton, Connection, DataAnalysis } from '@element-plus/icons-vue'
import { api } from './api'

const breadcrumbMap = {
  '/dashboard': { parent: '首页', current: '仪表盘' },
  '/servers': { parent: '首页', current: '服务器管理' },
  '/backups': { parent: '首页', current: '备份管理' },
  '/restore': { parent: '首页', current: '恢复操作' },
  '/mirrors': { parent: '首页', current: '镜像管理' },
  '/statistics': { parent: '首页', current: '统计分析' },
  '/settings': { parent: '首页', current: '系统设置' },
  '/schedules': { parent: '首页', current: '定时任务' },
}

export default {
  components: { LoginView, AlertBell, Monitor, Setting, FolderOpened, RefreshRight, Tools, Clock, Expand, Fold, SwitchButton, Connection, DataAnalysis },
  setup() {
    const router = useRouter()
    const route = useRoute()
    const authenticated = ref(false)
    const needsConfig = ref(false)
    const collapsed = ref(false)

    const activeMenu = computed(() => {
      const path = route.path
      if (path.startsWith('/servers/')) return '/servers'
      return path || '/dashboard'
    })

    const breadcrumbParent = computed(() => {
      const bc = breadcrumbMap[activeMenu.value]
      return bc ? bc.parent : '首页'
    })

    const breadcrumbCurrent = computed(() => {
      const bc = breadcrumbMap[activeMenu.value]
      return bc ? bc.current : ''
    })

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

    return { authenticated, needsConfig, collapsed, activeMenu, breadcrumbParent, breadcrumbCurrent, onLogin, logout }
  },
}
</script>

<style>
.layout { height: 100vh; }

.app-aside {
  background: linear-gradient(180deg, rgba(25,27,45,0.94) 0%, rgba(40,42,66,0.90) 100%);
  backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px);
  overflow: hidden; display: flex; flex-direction: column;
  border-right: 1px solid rgba(255,255,255,0.06);
  transition: width 0.3s ease;
}

.sidebar-logo {
  padding: 24px 20px 16px; border-bottom: 1px solid rgba(255,255,255,0.06);
  text-align: center;
}
.logo-text {
  font-size: 20px; font-weight: 700; letter-spacing: -0.3px; margin: 0;
  background: linear-gradient(135deg, #89f7fe, #667eea, #a18cd1);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  background-clip: text;
}
.logo-icon {
  font-size: 24px; font-weight: 700;
  background: linear-gradient(135deg, #89f7fe, #667eea);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  background-clip: text;
}
.logo-sub {
  font-size: 11px; color: rgba(255,255,255,0.35); letter-spacing: 2px; text-transform: uppercase;
  display: block; margin-top: 4px;
}

.collapse-btn {
  color: rgba(255,255,255,0.4); text-align: center; line-height: 36px; cursor: pointer;
  font-size: 16px; border-bottom: 1px solid rgba(255,255,255,0.06);
  user-select: none; transition: all 0.25s; display: flex; align-items: center;
  justify-content: center;
}
.collapse-btn:hover { color: #fff; background: rgba(255,255,255,0.06); }

.app-menu { border-right: none; flex: 1; padding: 8px 0; }
.el-menu-item {
  transition: all 0.25s; margin: 2px 10px; border-radius: 10px !important;
  height: 44px !important; line-height: 44px !important;
}
.el-menu-item.is-active {
  background: linear-gradient(135deg, rgba(102,126,234,0.25), rgba(118,75,162,0.2)) !important;
  box-shadow: inset 0 0 0 1px rgba(102,126,234,0.2);
  position: relative;
}
.el-menu-item.is-active::before {
  content: ''; position: absolute; left: -10px; top: 50%; transform: translateY(-50%);
  width: 3px; height: 20px; border-radius: 0 3px 3px 0;
  background: linear-gradient(135deg, #667eea, #764ba2);
  box-shadow: 0 0 12px rgba(102,126,234,0.5);
}
.el-menu-item:hover {
  background: rgba(255,255,255,0.06) !important; color: rgba(255,255,255,0.85) !important;
}

.sidebar-footer {
  padding: 16px 20px; border-top: 1px solid rgba(255,255,255,0.06);
  margin-top: auto;
}
.sidebar-user { display: flex; align-items: center; gap: 10px; }
.user-avatar {
  width: 32px; height: 32px; border-radius: 8px; flex-shrink: 0;
  background: linear-gradient(135deg, #667eea, #764ba2);
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-size: 14px; font-weight: 600;
}
.user-info { flex: 1; min-width: 0; }
.user-name { color: rgba(255,255,255,0.85); font-size: 13px; font-weight: 500; }
.user-role { color: rgba(255,255,255,0.35); font-size: 11px; }

.app-header {
  background: rgba(255,255,255,0.55); backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px);
  border-bottom: 1px solid rgba(0,0,0,0.04); box-shadow: 0 1px 12px rgba(0,0,0,0.03);
  display: flex; align-items: center; justify-content: space-between; padding: 0 28px;
  height: 60px !important;
}
.header-left { display: flex; align-items: center; gap: 12px; }
.breadcrumb { display: flex; align-items: center; gap: 6px; font-size: 13px; }
.breadcrumb-item { color: #9ca3af; }
.breadcrumb-sep { color: #d1d5db; }
.breadcrumb-current { color: #1a1a2e; font-weight: 600; }
.header-right { display: flex; align-items: center; gap: 12px; }
.logout-btn {
  background: rgba(255,255,255,0.5) !important; border: 1px solid rgba(0,0,0,0.06) !important;
  color: #6b7280 !important; border-radius: 8px !important;
}
.logout-btn:hover { background: rgba(239,68,68,0.06) !important; color: #ef4444 !important; border-color: rgba(239,68,68,0.2) !important; }

.app-main {
  background: linear-gradient(145deg, #f0f2f8 0%, #e8eaf6 30%, #f5f0ff 60%, #fdf2f8 100%);
  min-height: calc(100vh - 60px); padding: 24px 28px;
  position: relative;
}
.app-main::before {
  content: ''; position: absolute; inset: 0; opacity: 0.3; pointer-events: none;
  background-image: radial-gradient(circle at 1px 1px, rgba(102,126,234,0.06) 1px, transparent 0);
  background-size: 32px 32px;
}
.app-main > * { position: relative; z-index: 1; }

.blocking-card {
  display: flex; justify-content: center; align-items: center; height: calc(100vh - 120px);
}

.el-menu--collapse .el-menu-item {
  margin: 2px 6px !important;
}
</style>
