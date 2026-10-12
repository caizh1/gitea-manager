<template>
  <div v-if="!accessState.ready" class="session-loading"><el-skeleton :rows="5" animated /></div>
  <div v-else-if="!authenticated">
    <LoginView :sources="accessState.sources" @login="onLogin" />
  </div>
  <div v-else-if="accessState.user?.must_change_password" class="session-gate">
    <div class="glass-card password-card"><h2>设置安全的管理员密码</h2><p>初始密码需要修改后才能继续管理平台。</p>
      <el-form label-position="top" @submit.prevent="changePassword"><el-form-item label="当前密码"><el-input v-model="passwordForm.current_password" type="password" autocomplete="current-password" show-password /></el-form-item><el-form-item label="新密码"><el-input v-model="passwordForm.new_password" type="password" autocomplete="new-password" show-password /></el-form-item><el-form-item label="确认新密码"><el-input v-model="passwordForm.confirm" type="password" autocomplete="new-password" show-password /></el-form-item><el-alert v-if="passwordError" :title="passwordError" type="error" :closable="false" /><div class="session-actions"><el-button type="primary" native-type="submit" :loading="changingPassword">保存密码</el-button><el-button @click="logout">退出登录</el-button></div></el-form>
    </div>
  </div>
  <div v-else-if="!accessReady" class="session-gate"><el-result icon="info" title="尚未分配管理权限" :sub-title="`${accessState.user?.display_name || accessState.user?.username || ''}，请联系平台管理员分配组织。`"><template #extra><el-button @click="refreshSession">刷新授权</el-button><el-button @click="logout">退出登录</el-button></template></el-result></div>
  <div v-else class="layout">
    <el-container>
      <el-aside :width="collapsed ? '64px' : '240px'" class="app-aside" :class="{ 'is-collapsed': collapsed }">
        <div class="sidebar-logo">
          <h1 v-if="!collapsed" class="logo-text">Gitea Manager</h1>
          <span v-else class="logo-icon">G</span>
          <span v-if="!collapsed" class="logo-sub">Management Console</span>
        </div>
        <button type="button" class="collapse-btn" :aria-label="collapsed ? '展开导航' : '收起导航'" :aria-expanded="!collapsed" @click="collapsed = !collapsed">
          <el-icon :size="16"><component :is="collapsed ? 'Expand' : 'Fold'" /></el-icon>
        </button>
        <el-menu
          :default-active="activeMenu"
          :collapse="collapsed"
          router
          background-color="transparent"
          text-color="#5f6b7a"
          active-text-color="#1765be"
          class="app-menu"
        >
          <el-menu-item v-if="hasCapability('dashboard')" index="/dashboard" aria-label="仪表盘">
            <el-icon><Monitor /></el-icon>
            <template #title>仪表盘</template>
          </el-menu-item>
          <el-menu-item v-if="hasCapability('servers')" index="/servers" aria-label="服务器管理">
            <el-icon><Setting /></el-icon>
            <template #title>服务器管理</template>
          </el-menu-item>
          <el-menu-item v-if="hasCapability('backups')" index="/backups" aria-label="备份管理">
            <el-icon><FolderOpened /></el-icon>
            <template #title>备份管理</template>
          </el-menu-item>
          <el-menu-item v-if="hasCapability('restore')" index="/restore" aria-label="恢复操作">
            <el-icon><RefreshRight /></el-icon>
            <template #title>恢复操作</template>
          </el-menu-item>
          <el-menu-item v-if="hasCapability('settings')" index="/settings" aria-label="系统设置">
            <el-icon><Tools /></el-icon>
            <template #title>系统设置</template>
          </el-menu-item>
          <el-menu-item v-if="hasCapability('schedules')" index="/schedules" aria-label="定时任务">
            <el-icon><Clock /></el-icon>
            <template #title>定时任务</template>
          </el-menu-item>
          <el-menu-item v-if="hasCapability('mirrors')" index="/mirrors" aria-label="镜像管理">
            <el-icon><Connection /></el-icon>
            <template #title>镜像管理</template>
          </el-menu-item>
          <el-menu-item v-if="hasCapability('commit_gates')" index="/commit-gates" aria-label="提交门禁">
            <el-icon><Key /></el-icon>
            <template #title>提交门禁</template>
          </el-menu-item>
          <el-menu-item v-if="hasCapability('repository_policies')" index="/repository-policies" aria-label="仓库策略中心">
            <el-icon><Key /></el-icon>
            <template #title>仓库策略中心</template>
          </el-menu-item>
          <el-menu-item v-if="hasCapability('approval_rules')" index="/approval-rules" aria-label="审批规则">
            <el-icon><CircleCheck /></el-icon>
            <template #title>审批规则</template>
          </el-menu-item>
          <el-menu-item v-if="hasCapability('ai_reviews')" index="/ai-reviews" aria-label="AI Code Review">
            <el-icon><DataAnalysis /></el-icon>
            <template #title>AI Code Review</template>
          </el-menu-item>
          <el-menu-item v-if="hasCapability('statistics')" index="/statistics" aria-label="统计分析">
            <el-icon><DataAnalysis /></el-icon>
            <template #title>统计分析</template>
          </el-menu-item>
        <el-menu-item v-if="hasCapability('access')" index="/access" aria-label="账号与权限"><el-icon><User /></el-icon><template #title>账号与权限</template></el-menu-item>
        </el-menu>
        <div class="sidebar-footer">
          <div class="sidebar-user">
            <div class="user-avatar">{{ (accessState.user?.display_name || accessState.user?.username || '').slice(0, 1) }}</div>
            <div v-if="!collapsed" class="user-info">
              <div class="user-name">{{ accessState.user?.display_name || accessState.user?.username }}</div>
              <div class="user-role">{{ isPlatformAdmin ? '平台管理员' : '组织管理员' }}</div>
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
            <el-select v-if="!isPlatformAdmin" :model-value="accessState.organizationId" aria-label="当前管理组织" class="scope-select" @change="switchOrganization"><el-option v-for="org in accessState.organizations" :key="org.binding_id" :value="org.binding_id" :label="`${org.server_name} / ${org.organization_name}`" /></el-select>
            <AlertBell v-if="hasCapability('alerts')" :key="`alerts-${accessState.generation}`" />
            <el-button size="small" @click="logout" class="logout-btn">
              <el-icon><SwitchButton /></el-icon>
              <span>退出登录</span>
            </el-button>
          </div>
        </el-header>
        <ReviewAttentionBanner v-if="hasCapability('ai_reviews')" :key="`review-${accessState.generation}`" />
        <el-main class="app-main">
          <div v-if="needsConfig && !isSettingsPage" class="blocking-card">
            <el-result icon="warning" title="请先配置本机 IP" :sub-title="isPlatformAdmin ? '使用平台前需在系统设置中配置本机 IP，用于区分本地和远程服务器。' : '请联系平台管理员完成系统设置。'">
              <template #extra>
                <el-button v-if="isPlatformAdmin" type="primary" @click="$router.push('/settings')">前往系统设置</el-button>
              </template>
            </el-result>
          </div>
          <el-result v-else-if="!routeAllowed" icon="warning" title="无权访问此功能" sub-title="请从导航中选择已授权的功能。" />
          <router-view v-else :key="accessState.generation" v-slot="{ Component }">
            <transition name="page" mode="out-in">
              <component :is="Component" :key="`${accessState.generation}-${$route.path}`" />
            </transition>
          </router-view>
        </el-main>
      </el-container>
    </el-container>
  </div>
</template>

<script>
import { ref, reactive, computed, provide, onUnmounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessageBox, ElMessage } from 'element-plus'
import LoginView from './views/Login.vue'
import AlertBell from './components/AlertBell.vue'
import ReviewAttentionBanner from './components/ReviewAttentionBanner.vue'
import { Monitor, Setting, FolderOpened, RefreshRight, Tools, Clock, Expand, Fold, SwitchButton, Connection, DataAnalysis, Key, User } from '@element-plus/icons-vue'
import { api, cancelScopeRequests } from './api'
import { accessState, accessReady, isPlatformAdmin, hasCapability, applySession, setOrganization } from './composables/accessState.js'

const breadcrumbMap = {
  '/access': '账号与权限',
  '/dashboard': '仪表盘',
  '/servers': '服务器管理',
  '/backups': '备份管理',
  '/restore': '恢复操作',
  '/mirrors': '镜像管理',
  '/commit-gates': '提交门禁',
  '/repository-policies': '仓库策略中心',
  '/approval-rules': '审批规则',
  '/ai-reviews': 'AI Code Review',
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
  components: { LoginView, AlertBell, ReviewAttentionBanner, Monitor, Setting, FolderOpened, RefreshRight, Tools, Clock, Expand, Fold, SwitchButton, Connection, DataAnalysis, Key, User },
  setup() {
    const router = useRouter()
    const route = useRoute()
    const authenticated = computed(() => accessState.authenticated)
    const needsConfig = computed(() => accessState.needsConfig)
    const routeAllowed = computed(() => Boolean(route.meta.capability && hasCapability(route.meta.capability)))
    const passwordForm = reactive({ current_password: '', new_password: '', confirm: '' }), passwordError = ref(''), changingPassword = ref(false)
    let sessionTimer, refreshingSession = false
    async function refreshSession() {
      if (refreshingSession) return
      refreshingSession = true
      try { const { data } = await api.get('/session'); const generation = accessState.generation; applySession(data); if (generation !== accessState.generation) clearScope() }
      catch (error) { if (error.code !== 'ERR_CANCELED') expireSession() }
      finally { refreshingSession = false }
    }
    function clearScope() { cancelScopeRequests(); ElMessageBox.close(); ElMessage.closeAll() }
    function expireSession() { clearScope(); applySession({ sources: accessState.sources }); refreshSession() }
    function switchOrganization(id) { if (!setOrganization(id)) return; clearScope(); router.replace({ path: route.path.startsWith('/statistics/') ? '/statistics' : route.path, query: {} }) }
    async function changePassword() {
      if (changingPassword.value) return
      passwordError.value = ''
      if (!passwordForm.new_password || passwordForm.new_password !== passwordForm.confirm) { passwordError.value = '两次新密码不一致'; return }
      changingPassword.value = true
      try { await api.post('/access/password', { current_password: passwordForm.current_password, new_password: passwordForm.new_password }); Object.assign(passwordForm, { current_password: '', new_password: '', confirm: '' }); await refreshSession() }
      catch (error) { passwordError.value = error.response?.data?.error || '密码修改失败' }
      finally { changingPassword.value = false }
    }
    window.addEventListener('manager-session-expired', expireSession)
    window.addEventListener('manager-access-changed', refreshSession)
    onUnmounted(() => { clearInterval(sessionTimer); window.removeEventListener('manager-session-expired', expireSession); window.removeEventListener('manager-access-changed', refreshSession) })
    const compactViewport = window.matchMedia('(max-width: 1024px)')
    const collapsed = ref(compactViewport.matches)
    const syncSidebar = event => { collapsed.value = event.matches }
    compactViewport.addEventListener('change', syncSidebar)
    onUnmounted(() => compactViewport.removeEventListener('change', syncSidebar))

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

    async function checkSettings() { await refreshSession(); return !accessState.needsConfig }
    provide('refreshSettingsState', checkSettings)
    refreshSession()
    sessionTimer = setInterval(() => { if (accessState.authenticated && !document.hidden) refreshSession() }, 60000)
    async function onLogin() {
      await refreshSession()
      if (route.path !== '/ai-reviews' || route.query.attention !== '1') router.push('/dashboard')
    }
    async function logout() {
      try { await api.post('/logout'); clearScope(); applySession({ sources: accessState.sources }); await refreshSession(); router.push('/') } catch (error) { ElMessage.error(error.response?.data?.error || '退出未完成，请检查连接后再试') }
    }

    function goBreadcrumb(item) {
      if (!item.to) return
      router.push(item.to)
    }

    return { accessState, accessReady, isPlatformAdmin, hasCapability, routeAllowed, passwordForm, passwordError, changingPassword, changePassword, refreshSession, switchOrganization, authenticated, needsConfig, collapsed, activeMenu, breadcrumbItems, isSettingsPage, onLogin, logout, goBreadcrumb }
  },
}
</script>

<style>
.session-loading, .session-gate { min-height: 100dvh; display: flex; align-items: center; justify-content: center; padding: 24px; box-sizing: border-box; }
.password-card { width: 420px; max-width: 100%; padding: 28px; }
.password-card p { color: var(--text-secondary); margin-bottom: 20px; }
.session-actions { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 18px; }
.scope-select { width: 280px; max-width: 42vw; }
.user-name { overflow-wrap: anywhere; }
@media (max-width: 760px) { .app-header { height: auto; min-height: 70px; padding: 10px !important; flex-wrap: wrap; gap: 8px; } .header-right { flex-wrap: wrap; } .scope-select { max-width: 100%; width: 220px; } }

.layout { height: 100vh; height: 100dvh; }
.layout > .el-container { height: 100%; }
.layout .el-container { min-width: 0; min-height: 0; }

.app-aside {
  background: var(--glass-chrome);
  backdrop-filter: var(--blur-chrome); -webkit-backdrop-filter: var(--blur-chrome);
  overflow: hidden; display: flex; flex-direction: column;
  border-right: 1px solid rgba(15,23,42,0.08);
  box-shadow: inset -1px 0 0 rgba(255,255,255,0.58), 10px 0 26px rgba(15,23,42,0.045);
  transition: width 0.22s ease;
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
  appearance: none; background: transparent; border: 0; min-height: 36px; flex-shrink: 0;
  color: var(--text-muted); text-align: center; line-height: 36px; cursor: pointer;
  font-size: 16px; border-bottom: 1px solid rgba(15,23,42,0.06);
  user-select: none; transition: background-color 0.18s ease, color 0.18s ease; display: flex; align-items: center;
  justify-content: center;
}
.collapse-btn:hover { color: var(--color-primary); background: rgba(255,255,255,0.45); }

.app-menu { border-right: none; flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden; padding: 8px 0; }
.el-menu-item {
  transition: background-color 0.18s ease, color 0.18s ease, box-shadow 0.18s ease; margin: 3px 10px; border-radius: 16px !important;
  height: 44px !important; line-height: 44px !important;
  font-weight: 600;
}
.el-menu-item.is-active {
  background: rgba(0,122,255,0.12) !important;
  box-shadow: inset 3px 0 0 var(--color-primary), inset 0 0 0 1px rgba(0,122,255,0.10);
}
.el-menu-item:hover {
  background: rgba(255,255,255,0.48) !important; color: var(--text-primary) !important;
}

.sidebar-footer {
  flex-shrink: 0;
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
  background: var(--glass-chrome); backdrop-filter: var(--blur-chrome); -webkit-backdrop-filter: var(--blur-chrome);
  border-bottom: 1px solid rgba(15,23,42,0.07); box-shadow: 0 8px 22px rgba(15,23,42,0.045);
  display: flex; align-items: center; justify-content: space-between; padding: 0 28px;
  min-height: 60px; height: auto !important; flex-shrink: 0; gap: 12px;
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
  color: var(--text-muted); cursor: pointer; font: inherit; text-align: left; transition: background-color 0.18s ease, color 0.18s ease;
}
.breadcrumb-link:hover { background: rgba(0,122,255,0.08); color: var(--color-primary); }
.header-right { display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
.logout-btn {
  background: rgba(255,255,255,0.62) !important; border: 1px solid rgba(15,23,42,0.08) !important;
  color: var(--text-secondary) !important; border-radius: 14px !important;
}
.logout-btn:hover { background: rgba(239,68,68,0.06) !important; color: #ef4444 !important; border-color: rgba(239,68,68,0.2) !important; }

.app-main {
  background:
    radial-gradient(circle at 78% 18%, rgba(255,255,255,0.36) 0, rgba(255,255,255,0.16) 22%, transparent 44%),
    radial-gradient(circle at 24% 82%, rgba(255,255,255,0.30) 0, rgba(255,255,255,0.12) 18%, transparent 38%),
    var(--gradient-bg);
  min-height: 0; min-width: 0; padding: 24px 28px;
  position: relative; overflow: auto;
}
.app-main::before {
  content: ''; position: absolute; width: 320px; height: 320px; right: 8%; top: 10%;
  border-radius: 44% 56% 52% 48%; pointer-events: none;
  background: rgba(255,255,255,0.18);
  border: 1px solid rgba(255,255,255,0.30);
}
.app-main::after {
  content: ''; position: absolute; width: 220px; height: 220px; left: 18%; bottom: 9%;
  border-radius: 58% 42% 48% 52%; pointer-events: none;
  background: rgba(255,255,255,0.14);
  border: 1px solid rgba(255,255,255,0.24);
}
.app-main > * { position: relative; z-index: 1; }

.blocking-card {
  display: flex; justify-content: center; align-items: center; height: calc(100vh - 120px);
}

.el-menu--collapse .el-menu-item {
  margin: 2px 6px !important;
}
.is-collapsed .sidebar-logo { padding: 18px 8px; }
.is-collapsed .sidebar-footer { padding: 12px 16px; }
.is-collapsed .el-menu-item { padding: 0 !important; display: flex; justify-content: center; }
.is-collapsed .el-menu-item > .el-menu-tooltip__trigger { position: static; padding: 0; display: flex; justify-content: center; width: 100%; }
.is-collapsed .el-menu-item .el-icon { margin: 0; flex-shrink: 0; }
@media (max-width: 760px) {
  .app-header { padding: 10px 12px; flex-wrap: wrap; gap: 8px; }
  .app-header .header-left { flex: 1 1 140px; }
  .app-header .breadcrumb { max-width: 100%; flex-wrap: wrap; }
  .app-header .header-right { margin-left: auto; gap: 8px; }
  .app-main { padding: 16px 12px; }
  .app-main::before, .app-main::after { display: none; }
}
</style>
