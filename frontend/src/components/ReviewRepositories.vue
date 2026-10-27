<template>
  <section class="repository-manager" aria-labelledby="repository-title">
    <header class="repository-heading">
      <div><h2 id="repository-title">仓库管理</h2><p>管理已接入仓库的审查配置与事件通知。</p></div>
      <div class="heading-actions">
        <span class="global-status" :class="{ enabled: config.enabled }"><CircleCheck :size="17" />{{ config.enabled ? '全局已启用' : '全局已关闭' }}</span>
        <el-button class="refresh-button" aria-label="刷新仓库" :loading="refreshing" @click="$emit('refresh')"><RefreshCw v-if="!refreshing" :size="18" /></el-button>
        <el-button v-if="isPlatformAdmin" type="primary" class="connect-button" @click="setupVisible = true"><Plus :size="19" />接入仓库</el-button>
      </div>
    </header>

    <div class="repository-summary">
      <span v-if="isPlatformAdmin" class="model-summary"><Settings2 :size="20" /><span>当前模型</span><strong>{{ config.model || '尚未配置' }}</strong></span>
      <span class="repository-count"><Database :size="20" />{{ activeRepositories.length }} 个仓库 <span class="separator">·</span> {{ enabledCount }} 个已启用</span>
      <router-link v-if="isPlatformAdmin" to="/ai-reviews/settings" class="settings-link">模型与设置<ArrowUpRight :size="16" /></router-link>
      <span v-else class="organization-note">新增接入请联系平台管理员</span>
    </div>

    <div class="glass-card repository-panel" v-loading="refreshing">
      <div class="repository-filters">
        <el-input v-model="query" clearable placeholder="搜索仓库名称" aria-label="搜索仓库名称" class="repository-search"><template #prefix><Search :size="18" /></template></el-input>
        <el-select v-model="instance" clearable placeholder="全部实例" aria-label="筛选 Gitea 实例"><el-option v-for="server in repositoryServers" :key="server.id" :value="server.id" :label="server.name" /></el-select>
        <el-select v-model="status" clearable placeholder="全部状态" aria-label="筛选审查状态"><el-option label="已启用" value="enabled" /><el-option label="已关闭" value="disabled" /><el-option label="待处理" value="attention" /><el-option label="已归档" value="archived" /><el-option label="已移除" value="removed" /></el-select>
        <span class="filter-count">共 {{ filteredRepositories.length }} 个仓库</span>
      </div>

      <el-table :data="pageRepositories" class="repository-table" row-key="id" :row-class-name="({ row }) => row.removal_pending ? 'needs-attention' : ''" :empty-text="emptyText">
        <el-table-column label="仓库" min-width="235"><template #default="{ row }"><div class="repository-name" :title="row.repo_name">{{ row.repo_name }}</div><span class="repository-caption"><LockKeyhole v-if="row.private" :size="14" />{{ row.private ? '私有仓库' : row.private === false ? '公开仓库' : '审查仓库' }}</span></template></el-table-column>
        <el-table-column label="Gitea 实例" min-width="135"><template #default="{ row }"><span class="instance-label">{{ row.server_name }}</span></template></el-table-column>
        <el-table-column label="审查状态" min-width="130"><template #default="{ row }"><span class="review-status" :class="statusClass(row)"><Circle :size="10" fill="currentColor" />{{ statusLabel(row) }}</span></template></el-table-column>
        <el-table-column label="机器人" min-width="130"><template #default="{ row }"><span class="bot-name">{{ row.bot_name || '尚未配置' }}</span></template></el-table-column>
        <el-table-column label="事件通知" min-width="205"><template #default="{ row }">
          <div class="hook-status" :class="hookState(row).type"><component :is="hookState(row).icon" :size="18" /><span>{{ hookState(row).label }}</span></div>
          <div class="event-caption" :title="row.removal_error || row.reconcile_error || row.hook_status">{{ row.removal_error || row.reconcile_error || (row.removal_complete ? '历史审查已保留' : relativeEvent(row.last_event)) }}</div>
        </template></el-table-column>
        <el-table-column label="操作" width="125"><template #default="{ row }"><ReviewRepositoryActions :row="row" :enabled="config.enabled" @command="handleCommand" /></template></el-table-column>
      </el-table>

      <div class="repository-mobile-list" aria-label="审查仓库列表">
        <article v-for="row in pageRepositories" :key="row.id" class="repository-mobile-row">
          <div class="mobile-row-heading"><strong class="repository-name">{{ row.repo_name }}</strong><ReviewRepositoryActions :row="row" :enabled="config.enabled" @command="handleCommand" /></div>
          <div class="mobile-row-meta"><span class="instance-label">{{ row.server_name }}</span><span class="review-status" :class="statusClass(row)"><Circle :size="10" fill="currentColor" />{{ statusLabel(row) }}</span><span>{{ row.bot_name || '尚未配置机器人' }}</span></div>
          <div class="hook-status" :class="hookState(row).type"><component :is="hookState(row).icon" :size="17" /><span>{{ hookState(row).label }}</span><span class="mobile-event-time">{{ relativeEvent(row.last_event) }}</span></div>
          <p v-if="row.removal_error || row.reconcile_error" class="mobile-row-error">{{ row.removal_error || row.reconcile_error }}</p>
        </article>
        <el-empty v-if="!pageRepositories.length" :description="emptyText" :image-size="64" />
      </div>

      <footer class="repository-footer"><span>{{ filteredRepositories.length ? `显示 ${firstRow}–${lastRow} 条，共 ${filteredRepositories.length} 条` : '没有匹配的仓库' }}</span><el-pagination v-model:current-page="page" :page-size="pageSize" :total="filteredRepositories.length" layout="prev, pager, next" :hide-on-single-page="false" /></footer>
    </div>
    <p class="repository-help"><Info :size="17" />{{ isPlatformAdmin ? '新增接入会自动检查机器人权限和事件通知。移除接入保留历史审查与 Gitea 仓库。' : '关闭审查可暂停该仓库的新任务，历史记录继续保留。' }}</p>

    <el-drawer v-if="isPlatformAdmin" v-model="setupVisible" title="接入审查仓库" size="min(600px, 100vw)" append-to-body class="repository-setup-drawer">
      <AIReviewSetup embedded :servers="servers" :config="config" :repositories="activeRepositories" @changed="$emit('refresh')" />
      <div class="manual-entry"><span>已有专用机器人？</span><el-button link type="primary" @click="setupVisible = false; $emit('configure', null)">手动配置仓库</el-button></div>
    </el-drawer>

    <el-dialog v-model="removeVisible" title="移除审查接入" width="min(520px, 94vw)" append-to-body :close-on-click-modal="!removing" :close-on-press-escape="!removing" :show-close="!removing">
      <p class="remove-repository-name">{{ removalTarget?.server_name }} / {{ removalTarget?.repo_name }}</p>
      <p class="remove-explanation">停止新审查并清理该接入的事件通知。历史审查、Gitea 仓库、机器人账号及共享的进度组件均保留。</p>
      <el-alert v-if="removeError" :title="removeError" type="error" :closable="false" show-icon />
      <p class="remove-explanation">有运行中或发布待核对的任务时，需要先在审查工作区处理。若通知清理失败，仓库保持停止接入，可继续重试。</p>
      <template #footer><el-button :disabled="removing" @click="removeVisible = false">取消</el-button><el-button type="danger" :loading="removing" @click="removeRepository">{{ removalTarget?.removal_pending ? '重试移除' : '移除接入' }}</el-button></template>
    </el-dialog>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowUpRight, Circle, CircleAlert, CircleCheck, Database, Info, LockKeyhole, Plus, RefreshCw, Search, Settings2 } from '@lucide/vue'
import { api } from '../api'
import { isPlatformAdmin } from '../composables/accessState'
import { errorMessage } from './reviewDisplay'
import AIReviewSetup from './AIReviewSetup.vue'
import ReviewRepositoryActions from './ReviewRepositoryActions.vue'

const props = defineProps({ repositories: { type: Array, default: () => [] }, servers: { type: Array, default: () => [] }, config: { type: Object, default: () => ({}) }, refreshing: Boolean })
const emit = defineEmits(['refresh', 'configure', 'webhook', 'review', 'download'])
const query = ref(''), instance = ref(null), status = ref(''), page = ref(1), pageSize = 10, setupVisible = ref(false)
const activeRepositories = computed(() => props.repositories.filter(row => !row.access_archived))
const enabledCount = computed(() => activeRepositories.value.filter(row => row.enabled).length)
const repositoryServers = computed(() => [...new Map(props.repositories.map(row => [row.server_id, { id: row.server_id, name: row.server_name }])).values()])
const filteredRepositories = computed(() => props.repositories.filter(row => {
  if (instance.value && row.server_id !== instance.value) return false
  if (!row.repo_name.toLocaleLowerCase().includes(query.value.trim().toLocaleLowerCase())) return false
  if (status.value === 'removed') return row.removal_complete
  if (status.value === 'archived') return row.access_archived && !row.removal_pending && !row.removal_complete
  if (row.removal_complete) return false
  if (status.value === 'attention') return row.removal_pending || (!row.access_archived && hookState(row).type === 'warning')
  if (status.value === 'enabled') return !row.access_archived && row.enabled
  if (status.value === 'disabled') return !row.access_archived && !row.enabled
  return true
}))
const pageRepositories = computed(() => filteredRepositories.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const firstRow = computed(() => (page.value - 1) * pageSize + 1)
const lastRow = computed(() => Math.min(page.value * pageSize, filteredRepositories.value.length))
const emptyText = computed(() => query.value || instance.value || status.value ? '没有匹配的仓库，请调整筛选条件' : '尚未接入仓库')
watch([query, instance, status], () => { page.value = 1 })
watch(() => filteredRepositories.value.length, length => { page.value = Math.max(1, Math.min(page.value, Math.ceil(length / pageSize))) })
function statusLabel(row) { return row.removal_pending ? '移除待完成' : row.removal_complete ? '已移除' : row.access_archived ? '已归档' : row.enabled ? '已启用' : '已关闭' }
function statusClass(row) { return row.removal_pending ? 'warning' : !row.access_archived && row.enabled ? 'enabled' : 'disabled' }
function hookState(row) {
  if (row.removal_complete) return { type: 'disabled', label: '已移除', icon: CircleCheck }
  if (row.removal_pending) return { type: 'warning', label: '清理待完成', icon: CircleAlert }
  if (row.access_archived) return { type: 'disabled', label: '历史记录', icon: Info }
  if (row.reconcile_error || /失败|不确定|待核对/.test(row.hook_status || '')) return { type: 'warning', label: '待检查', icon: CircleAlert }
  if (row.hook_id && row.last_event) return { type: 'enabled', label: '已验证', icon: CircleCheck }
  if (row.hook_id) return { type: 'enabled', label: '已安装', icon: CircleCheck }
  return { type: 'warning', label: '未安装', icon: CircleAlert }
}
function relativeEvent(timestamp) {
  if (!timestamp) return '尚未收到事件'
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - timestamp))
  if (seconds < 60) return '刚刚收到'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前收到`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前收到`
  return `${Math.floor(seconds / 86400)} 天前收到`
}
function handleCommand(command, row) {
  if (command === 'remove') openRemove(row)
  else emit(command, row)
}
const removeVisible = ref(false), removalTarget = ref(null), removing = ref(false), removeError = ref('')
function openRemove(row) { removalTarget.value = row; removeError.value = ''; removeVisible.value = true }
async function removeRepository() {
  if (removing.value) return
  removing.value = true; removeError.value = ''
  try {
    const token = (await api.get('/ai-review/csrf')).data.token
    await api.delete(`/ai-review/repositories/${removalTarget.value.id}`, { data: { revision: removalTarget.value.revision }, headers: { 'X-Review-CSRF': token }, notifyError: false })
    removeVisible.value = false; ElMessage.success('接入已移除，历史审查与 Gitea 仓库已保留')
  } catch (error) { removeError.value = errorMessage(error) }
  finally { removing.value = false; emit('refresh') }
}
watch(() => props.repositories, rows => {
  if (removalTarget.value) removalTarget.value = rows.find(row => row.id === removalTarget.value.id) || removalTarget.value
})
</script>

<style scoped>
.repository-manager { min-width: 0; padding: 0 8px; color: var(--text-primary, #172334); }
.repository-heading { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 20px; margin: 10px 0 26px; padding: 0 18px; }
.repository-heading h2 { font-size: 36px; line-height: 1.25; letter-spacing: -.5px; margin: 0 0 10px; font-weight: 700; }
.repository-heading p { margin: 0; font-size: 15px; color: var(--text-secondary, #64748b); }
.heading-actions, .global-status, .model-summary, .repository-count, .settings-link, .repository-caption, .review-status, .hook-status, .repository-help { display: flex; align-items: center; gap: 9px; min-width: 0; }
.heading-actions { flex-wrap: wrap; gap: 14px; }
.heading-actions .el-button + .el-button { margin-left: 0; }
.global-status { color: #65758a; font-size: 13px; padding: 10px 13px; background: #eef2f6; border-radius: 12px; white-space: nowrap; }
.global-status.enabled { background: #eaf6ee; color: #298843; }
.refresh-button { width: 42px; height: 42px; padding: 0; flex: 0 0 42px; }
.connect-button { height: 42px; gap: 8px; padding: 0 18px; }
.repository-summary { display: flex; align-items: center; gap: 25px; flex-wrap: wrap; padding: 18px 22px; margin-bottom: 20px; border-radius: 14px; background: rgba(255,255,255,.65); font-size: 14px; color: #52647e; }
.model-summary { flex-wrap: wrap; overflow-wrap: anywhere; }
.model-summary strong { color: #1765be; font-weight: 600; }
.repository-count { flex-wrap: wrap; }
.separator { color: #93a0b1; }
.settings-link { margin-left: auto; text-decoration: none; color: #1765be; font-weight: 500; white-space: nowrap; }
.organization-note { margin-left: auto; color: #64748b; font-size: 13px; }
.repository-panel { padding: 18px 18px 0; border-radius: 16px; }
.repository-filters { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; padding: 0 4px 18px; }
.repository-search { flex: 0 1 350px; min-width: 180px; }
.repository-filters .el-select { width: 178px; max-width: 100%; }
.repository-filters :deep(.el-input__wrapper), .repository-filters :deep(.el-select__wrapper) { min-height: 40px; box-sizing: border-box; }
.filter-count { font-size: 13px; color: #70809a; margin-left: auto; white-space: nowrap; }
.repository-table { --el-table-border-color: #e9eff5; --el-table-header-bg-color: #f0f5fb; --el-table-bg-color: transparent; --el-table-tr-bg-color: transparent; --el-table-row-hover-bg-color: #f5f9fd; background: transparent; }
.repository-table :deep(th.el-table__cell) { height: 50px; font-size: 13px; font-weight: 600; color: #54637a; }
.repository-table :deep(td.el-table__cell) { padding: 14px 0; height: 72px; box-sizing: border-box; }
.repository-table :deep(.cell) { padding-left: 20px; padding-right: 12px; line-height: 1.5; }
.repository-table :deep(.needs-attention) { --el-table-tr-bg-color: #fffaf1; }
.repository-name { font-size: 16px; font-weight: 600; color: #14213b; overflow-wrap: anywhere; }
.repository-caption { margin-top: 4px; color: #7b899f; font-size: 12px; }
.instance-label { display: inline-flex; max-width: 100%; padding: 5px 10px; border-radius: 6px; background: #eaf3ff; color: #1765be; font-size: 13px; overflow-wrap: anywhere; }
.review-status { font-size: 14px; white-space: nowrap; }
.review-status.enabled > svg { color: #31a153; }
.review-status.disabled > svg { color: #a1abbc; }
.review-status.warning > svg, .hook-status.warning { color: #c87905; }
.hook-status.enabled > svg { color: #31a153; }
.hook-status.disabled { color: #7b899f; }
.bot-name { font-size: 14px; overflow-wrap: anywhere; color: #47566d; }
.event-caption { color: #7b899f; font-size: 12px; line-height: 1.6; margin: 4px 0 0 27px; overflow-wrap: anywhere; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.repository-mobile-list { display: none; }
.repository-footer { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; min-height: 70px; padding: 10px 10px; color: #7b899f; font-size: 13px; }
.repository-footer :deep(.el-pagination) { margin: 0; --el-pagination-bg-color: transparent; }
.repository-help { font-size: 12px; line-height: 1.7; color: #70809a; padding: 16px 20px 0; margin: 0; align-items: flex-start; }
.repository-help svg { flex: 0 0 17px; color: #1765be; margin-top: 2px; }
.manual-entry { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; padding: 20px 0; border-top: 1px solid #e6edf5; font-size: 13px; color: #6b7d94; }
.remove-repository-name { font-size: 16px; font-weight: 600; overflow-wrap: anywhere; }
.remove-explanation { line-height: 1.8; color: #62728a; }
@media (max-width: 900px) { .repository-manager { padding: 0; }.repository-heading { padding: 0; }.repository-summary { gap: 14px; padding: 16px; }.repository-heading h2 { font-size: 28px; }.repository-filters { gap: 10px; }.repository-search { flex: 1 1 100%; }.repository-filters .el-select { flex: 1 1 140px; }.filter-count { margin-left: 0; }.repository-panel { padding: 14px 10px 0; } }
@media (max-width: 540px) { .repository-heading { margin-top: 0; gap: 16px; }.heading-actions { width: 100%; gap: 10px; }.connect-button { margin-left: auto !important; }.repository-summary { font-size: 13px; }.settings-link, .organization-note { margin-left: 0; }.repository-heading p { font-size: 13px; line-height: 1.7; }.repository-footer { padding-left: 4px; padding-right: 4px; }.repository-help { padding: 14px 4px 0; } }
@media (max-width: 640px) {
  .repository-table { display: none; }.repository-mobile-list { display: block; }
  .repository-mobile-row { padding: 18px 4px; border-bottom: 1px solid #e9eff5; }
  .mobile-row-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
  .mobile-row-heading .repository-name { min-width: 0; flex: 1; font-size: 15px; padding-top: 6px; }
  .mobile-row-meta { display: flex; align-items: center; gap: 10px 14px; flex-wrap: wrap; margin: 12px 0; color: #6b7d94; font-size: 12px; }
  .mobile-row-meta .review-status, .mobile-row-meta .instance-label { font-size: 12px; }
  .repository-mobile-row .hook-status { font-size: 13px; flex-wrap: wrap; }
  .mobile-event-time { color: #7b899f; font-size: 12px; }
  .mobile-row-error { color: #a66c12; font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
}
</style>
