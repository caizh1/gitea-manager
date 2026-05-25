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
          text-color="#5f6b7a"
          active-text-color="#007aff"
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
          <el-menu-item index="/commit-gates">
            <el-icon><Key /></el-icon>
            <template #title>提交门禁</template>
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
              <template v-for="(item, index) in breadcrumbItems" :key="`${item.label}-${index}`">
                <span v-if="index > 0" class="breadcrumb-sep">/</span>
                <button
                  v-if="item.to"
                  type="button"
                  class="breadcrumb-link"
                  @click="goBreadcrumb(item)"
                >
                  {{ item.label }}
                </button>
                <span v-else class="breadcrumb-current" :class="{ 'is-muted': index === 0 }">
                  {{ item.label }}
                </span>
              </template>
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
          <div v-if="needsConfig && !isSettingsPage" class="blocking-card">
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
import { ref, computed, provide, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import LoginView from './views/Login.vue'
import AlertBell from './components/AlertBell.vue'
import { Monitor, Setting, FolderOpened, RefreshRight, Tools, Clock, Expand, Fold, SwitchButton, Connection, DataAnalysis, Key } from '@element-plus/icons-vue'
import { api } from './api'

const breadcrumbMap = {
  '/dashboard': '仪表盘',
  '/servers': '服务器管理',
  '/backups': '备份管理',
  '/restore': '恢复操作',
  '/mirrors': '镜像管理',
  '/commit-gates': '提交门禁',
  '/statistics': '统计分析',
  '/settings': '系统设置',
  '/schedules': '定时任务',
}

function decodeRouteParam(value) {
  const raw = Array.isArray(value) ? value[0] : value
  if (!raw) return ''
  try {
    return decodeURIComponent(raw)
  } catch (e) {
    return raw
  }
}

export default {
  components: { LoginView, AlertBell, Monitor, Setting, FolderOpened, RefreshRight, Tools, Clock, Expand, Fold, SwitchButton, Connection, DataAnalysis, Key },
  setup() {
    const router = useRouter()
    const route = useRoute()
    const authenticated = ref(false)
    const needsConfig = ref(false)
    const collapsed = ref(false)

    const activeMenu = computed(() => {
      const path = route.path
      if (path.startsWith('/servers/')) return '/servers'
      if (path.startsWith('/statistics')) return '/statistics'
      return path || '/dashboard'
    })

    const breadcrumbItems = computed(() => {
      const path = route.path
      const serverQuery = route.query.server_id ? { server_id: route.query.server_id } : {}

      if (path === '/statistics') {
        return [
          { label: '首页' },
          { label: '统计分析' },
        ]
      }

      if (path === '/statistics/authors') {
        return [
          { label: '首页' },
          { label: '统计分析', to: { path: '/statistics', query: serverQuery } },
          { label: '作者贡献排行' },
        ]
      }

      if (path.startsWith('/statistics/authors/')) {
        return [
          { label: '首页' },
          { label: '统计分析', to: { path: '/statistics', query: serverQuery } },
          { label: '作者贡献排行', to: { path: '/statistics/authors', query: serverQuery } },
          { label: decodeRouteParam(route.params.name) },
        ]
      }

      const current = breadcrumbMap[activeMenu.value] || ''
      return [
        { label: '首页' },
        { label: current },
      ]
    })

    const isSettingsPage = computed(() => route.path === '/settings')

    function checkSettings() {
      return api.get('/settings').then(res => {
        needsConfig.value = !res.data.host_ip
        return !needsConfig.value
      }).catch(() => { needsConfig.value = false })
    }

    provide('refreshSettingsState', checkSettings)

    api.get('/session').then(res => {
      authenticated.value = res.data.authenticated
      if (res.data.authenticated) checkSettings()
    }).catch(() => {
      authenticated.value = false
    })

    function onLogin() {
      authenticated.value = true
      checkSettings()
      router.push('/dashboard')
    }

    watch(() => route.path, (path) => {
      if (authenticated.value && path !== '/settings') {
        checkSettings()
      }
    })

    function logout() {
      api.post('/logout').then(() => {
        authenticated.value = false
        router.push('/')
      })
    }

    function goBreadcrumb(item) {
      if (!item.to) return
      router.push(item.to)
    }

    return { authenticated, needsConfig, collapsed, activeMenu, breadcrumbItems, isSettingsPage, onLogin, logout, goBreadcrumb }
  },
}
</script>

<style>
.layout { height: 100vh; }

.app-aside {
  background: rgba(255,255,255,0.58);
  backdrop-filter: blur(36px) saturate(180%); -webkit-backdrop-filter: blur(36px) saturate(180%);
  overflow: hidden; display: flex; flex-direction: column;
  border-right: 1px solid rgba(15,23,42,0.08);
  box-shadow: inset -1px 0 0 rgba(255,255,255,0.62), 14px 0 42px rgba(15,23,42,0.06);
  transition: width 0.3s ease;
}

.sidebar-logo {
  padding: 24px 20px 16px; border-bottom: 1px solid rgba(15,23,42,0.06);
  text-align: center;
}
.logo-text {
  font-size: 20px; font-weight: 760; letter-spacing: 0; margin: 0;
  color: var(--text-primary);
}
.logo-icon {
  font-size: 24px; font-weight: 760;
  color: var(--color-primary);
}
.logo-sub {
  font-size: 11px; color: var(--text-muted); letter-spacing: 2px; text-transform: uppercase;
  display: block; margin-top: 4px;
}

.collapse-btn {
  color: var(--text-muted); text-align: center; line-height: 36px; cursor: pointer;
  font-size: 16px; border-bottom: 1px solid rgba(15,23,42,0.06);
  user-select: none; transition: all 0.25s; display: flex; align-items: center;
  justify-content: center;
}
.collapse-btn:hover { color: var(--color-primary); background: rgba(255,255,255,0.45); }

.app-menu { border-right: none; flex: 1; padding: 8px 0; }
.el-menu-item {
  transition: all 0.25s; margin: 3px 10px; border-radius: 16px !important;
  height: 44px !important; line-height: 44px !important;
  font-weight: 600;
}
.el-menu-item.is-active {
  background: rgba(0,122,255,0.12) !important;
  box-shadow: inset 0 0 0 1px rgba(0,122,255,0.10), inset 0 1px 0 rgba(255,255,255,0.72);
  position: relative;
}
.el-menu-item.is-active::before {
  content: ''; position: absolute; left: -10px; top: 50%; transform: translateY(-50%);
  width: 3px; height: 20px; border-radius: 0 999px 999px 0;
  background: var(--color-primary);
  box-shadow: 0 0 14px rgba(0,122,255,0.32);
}
.el-menu-item:hover {
  background: rgba(255,255,255,0.48) !important; color: var(--text-primary) !important;
}

.sidebar-footer {
  padding: 16px 20px; border-top: 1px solid rgba(15,23,42,0.06);
  margin-top: auto;
}
.sidebar-user { display: flex; align-items: center; gap: 10px; }
.user-avatar {
  width: 32px; height: 32px; border-radius: 50%; flex-shrink: 0;
  background: rgba(0,122,255,0.12);
  border: 1px solid rgba(0,122,255,0.16);
  display: flex; align-items: center; justify-content: center;
  color: var(--color-primary); font-size: 14px; font-weight: 700;
}
.user-info { flex: 1; min-width: 0; }
.user-name { color: var(--text-primary); font-size: 13px; font-weight: 600; }
.user-role { color: var(--text-muted); font-size: 11px; }

.app-header {
  background: rgba(255,255,255,0.58); backdrop-filter: blur(34px) saturate(180%); -webkit-backdrop-filter: blur(34px) saturate(180%);
  border-bottom: 1px solid rgba(15,23,42,0.07); box-shadow: 0 10px 34px rgba(15,23,42,0.06);
  display: flex; align-items: center; justify-content: space-between; padding: 0 28px;
  height: 60px !important;
}
.header-left { display: flex; align-items: center; gap: 12px; min-width: 0; }
.breadcrumb {
  display: flex; align-items: center; gap: 6px; font-size: 13px;
  min-width: 0; max-width: clamp(180px, calc(100vw - 460px), 640px);
}
.breadcrumb-sep { color: rgba(95,107,122,0.42); }
.breadcrumb-current,
.breadcrumb-link {
  min-width: 0; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.breadcrumb-current { color: var(--text-primary); font-weight: 700; }
.breadcrumb-current.is-muted { color: var(--text-muted); font-weight: 500; }
.breadcrumb-link {
  appearance: none; padding: 2px 5px; border: none; border-radius: 8px; background: transparent;
  color: var(--text-muted); cursor: pointer; font: inherit; text-align: left; transition: all 0.2s;
}
.breadcrumb-link:hover { background: rgba(0,122,255,0.08); color: var(--color-primary); }
.header-right { display: flex; align-items: center; gap: 12px; }
.logout-btn {
  background: rgba(255,255,255,0.62) !important; border: 1px solid rgba(15,23,42,0.08) !important;
  color: var(--text-secondary) !important; border-radius: 14px !important;
}
.logout-btn:hover { background: rgba(239,68,68,0.06) !important; color: #ef4444 !important; border-color: rgba(239,68,68,0.2) !important; }

.app-main {
  background: var(--gradient-bg);
  min-height: calc(100vh - 60px); padding: 24px 28px;
  position: relative; overflow: hidden;
}
.app-main::before {
  content: ''; position: absolute; width: 380px; height: 380px; right: 7%; top: 8%;
  border-radius: 44% 56% 52% 48%; pointer-events: none;
  background: rgba(255,255,255,0.32);
  box-shadow: inset 24px 28px 82px rgba(255,255,255,0.72), inset -24px -28px 70px rgba(148,163,184,0.12);
}
.app-main::after {
  content: ''; position: absolute; width: 240px; height: 240px; left: 18%; bottom: 8%;
  border-radius: 58% 42% 48% 52%; pointer-events: none;
  background: rgba(255,255,255,0.24);
  box-shadow: inset 18px 22px 64px rgba(255,255,255,0.68), inset -18px -22px 58px rgba(148,163,184,0.12);
}
.app-main > * { position: relative; z-index: 1; }

.blocking-card {
  display: flex; justify-content: center; align-items: center; height: calc(100vh - 120px);
}

.el-menu--collapse .el-menu-item {
  margin: 2px 6px !important;
}
</style>
