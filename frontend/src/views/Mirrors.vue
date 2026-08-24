<template>
  <div>
    <div class="section-header">
      <h3 class="section-title">镜像管理</h3>
      <el-button type="primary" size="small" @click="showCreateDialog = true">新建镜像配置</el-button>
    </div>

    <div v-if="mirrors.length === 0" class="glass-card" style="padding:40px;text-align:center;">
      <el-empty description="暂无镜像配置">
        <el-button type="primary" @click="showCreateDialog = true">创建镜像</el-button>
      </el-empty>
    </div>

    <div v-else class="mirror-list">
      <div v-for="m in mirrors" :key="m.id" class="glass-card mirror-card">
        <div class="mirror-card-header">
          <div class="mirror-path">
            <span class="mirror-server-name">{{ m.source_server_name }}</span>
            <span class="mirror-arrow">→</span>
            <span class="mirror-server-name">{{ m.target_server_name }}</span>
          </div>
          <div class="mirror-status-area">
            <el-tag v-if="m.status === 'success'" type="success" size="small" effect="dark">正常</el-tag>
            <el-tag v-else-if="m.status === 'partial'" type="warning" size="small" effect="dark">部分失败</el-tag>
            <el-tag v-else-if="m.status === 'syncing'" type="warning" size="small" effect="dark">同步中</el-tag>
            <el-tag v-else-if="m.status === 'failed'" type="danger" size="small" effect="dark">失败</el-tag>
            <el-tag v-else type="info" size="small" effect="dark">{{ m.status }}</el-tag>
          </div>
        </div>
        <div class="mirror-card-info">
          <span class="mirror-info-item">同步模式: {{ m.deprecated ? '旧 Pull Mirror（已弃用）' : 'Push Mirror' }}</span>
          <span class="mirror-info-item">Push 后立即同步: {{ m.sync_on_commit !== false ? '开启' : '关闭' }}</span>
          <span class="mirror-info-item">兜底间隔: {{ m.sync_interval }}分钟</span>
          <span class="mirror-info-item">仓库: {{ m.synced_repos }}/{{ m.total_repos }} 同步</span>
          <span v-if="m.failed_repos > 0" class="mirror-info-item mirror-fail">失败: {{ m.failed_repos }}</span>
          <span v-if="m.last_sync_at" class="mirror-info-item">最后同步: {{ fmt(m.last_sync_at) }}</span>
        </div>
        <div v-if="isMirrorRunning(m)" class="mirror-progress">
          <div class="progress-header">
            <span class="progress-label">{{ mirrorProgressLabel(m) }}</span>
            <span class="progress-pct">{{ mirrorProgressPct(m) }}%</span>
          </div>
          <el-progress
            :percentage="mirrorProgressPct(m)"
            :status="mirrorProgressStatus(m)"
            :stroke-width="7"
            :show-text="false"
          />
          <div v-if="mirrorProgressDetail(m)" class="progress-detail">{{ mirrorProgressDetail(m) }}</div>
        </div>
        <el-alert
          v-if="m.deprecated"
          type="warning"
          :closable="false"
          show-icon
          title="旧 Pull Mirror / Git Clone 配置已弃用，请删除后重新创建 Push Mirror。"
          style="margin-bottom:14px"
        />
        <pre v-if="mirrorFailureLog(m)" class="mirror-log">{{ mirrorFailureLog(m) }}</pre>
        <div class="mirror-card-actions">
          <el-button size="small" type="primary" @click="doSync(m.id)" :disabled="m.status === 'syncing' || m.deprecated">
            {{ m.status === 'syncing' ? '同步中...' : '同步' }}
          </el-button>
          <el-button size="small" type="success" @click="repairMirror(m.id)" :disabled="m.status === 'syncing' || m.deprecated">
            刷新/修复
          </el-button>
          <el-button size="small" @click="showDetail(m)">查看详情</el-button>
          <el-button size="small" @click="editMirror(m)" :disabled="m.deprecated">编辑</el-button>
          <el-popconfirm title="确认删除此镜像配置?" @confirm="deleteMirror(m.id)">
            <template #reference>
              <el-button size="small" type="danger">删除</el-button>
            </template>
          </el-popconfirm>
        </div>
      </div>
    </div>

    <el-dialog v-model="showCreateDialog" :title="editingMirror ? '编辑镜像配置' : '新建镜像配置'" width="520px" @close="resetForm">
      <el-form label-width="110px">
        <el-form-item label="源服务器">
          <el-select v-model="form.source_server_id" placeholder="选择主服务器" style="width:100%" :disabled="!!editingMirror">
            <el-option v-for="s in primaryServers" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="目标服务器">
          <el-select v-model="form.target_server_id" placeholder="选择镜像服务器" style="width:100%" :disabled="!!editingMirror">
            <el-option v-for="s in nonPrimaryServers" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="镜像模式">
          <el-tag type="success" effect="dark">Push Mirror</el-tag>
        </el-form-item>
        <el-form-item label="Push 后同步">
          <el-switch v-model="form.sync_on_commit" active-text="开启" inactive-text="关闭" />
        </el-form-item>
        <el-form-item label="兜底间隔(分钟)">
          <el-input-number v-model="form.sync_interval" :min="5" :max="1440" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreateDialog = false">取消</el-button>
        <el-button v-if="!editingMirror" type="primary" @click="createMirror" :disabled="!form.source_server_id || !form.target_server_id">创建</el-button>
        <el-button v-else type="primary" @click="updateMirror">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="detailVisible" title="镜像仓库详情" width="min(1120px, 92vw)" class="mirror-detail-dialog">
      <div class="detail-shell">
        <div v-if="detailMirror && isMirrorRunning(detailMirror)" class="mirror-progress detail-progress">
          <div class="progress-header">
            <span class="progress-label">{{ mirrorProgressLabel(detailMirror) }}</span>
            <span class="progress-pct">{{ mirrorProgressPct(detailMirror) }}%</span>
          </div>
          <el-progress
            :percentage="mirrorProgressPct(detailMirror)"
            :status="mirrorProgressStatus(detailMirror)"
            :stroke-width="8"
            :show-text="false"
          />
          <div v-if="mirrorProgressDetail(detailMirror)" class="progress-detail">{{ mirrorProgressDetail(detailMirror) }}</div>
        </div>
        <pre v-if="detailSyncLog" class="mirror-log detail-log">{{ detailSyncLog }}</pre>

        <el-tabs v-model="detailTab" class="detail-tabs">
          <el-tab-pane label="仓库状态" name="repos">
            <div class="detail-header">
              <span>仓库状态列表</span>
              <button class="icon-btn-sm" @click="loadDetailRepo" title="刷新仓库状态" aria-label="刷新仓库状态">
                <el-icon><Refresh /></el-icon>
              </button>
            </div>
            <el-table :data="detailRepos" stripe size="small" class="compact-table" max-height="460">
              <el-table-column prop="repo_name" label="仓库" min-width="240" show-overflow-tooltip />
              <el-table-column prop="status" label="状态" width="112">
                <template #default="{ row }">
                  <el-tag :type="repoStatusType(row.status)" size="small" effect="dark" class="status-tag">{{ repoStatusLabel(row.status) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="sync_mode" label="模式" width="120">
                <template #default="{ row }">
                  {{ row.sync_mode === 'push_mirror' ? 'Push Mirror' : row.sync_mode || '旧配置' }}
                </template>
              </el-table-column>
              <el-table-column prop="last_sync_at" label="最后同步" width="170">
                <template #default="{ row }">{{ fmt(row.last_sync_at) }}</template>
              </el-table-column>
              <el-table-column prop="error_msg" label="错误" min-width="260" show-overflow-tooltip />
              <el-table-column label="操作" width="96" align="center">
                <template #default="{ row }">
                  <el-button v-if="row.status === 'failed' && !detailDeprecated" size="small" text type="primary" @click="syncOneRepo(row)">重试</el-button>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>

          <el-tab-pane label="审计记录" name="audit">
            <div class="detail-header">
              <span>审计记录</span>
              <button class="icon-btn-sm" @click="loadAuditLogs" title="刷新审计记录" aria-label="刷新审计记录">
                <el-icon><Refresh /></el-icon>
              </button>
            </div>
            <el-table :data="auditLogs" stripe size="small" class="compact-table" max-height="460" empty-text="暂无审计记录">
              <el-table-column prop="created_at" label="时间" width="170">
                <template #default="{ row }">{{ fmt(row.created_at) }}</template>
              </el-table-column>
              <el-table-column prop="repo_name" label="仓库" min-width="190" show-overflow-tooltip />
              <el-table-column prop="action" label="动作" width="150">
                <template #default="{ row }">{{ actionLabel(row.action) }}</template>
              </el-table-column>
              <el-table-column prop="status" label="结果" width="96">
                <template #default="{ row }">
                  <el-tag :type="auditStatusType(row.status)" size="small" effect="dark" class="status-tag">{{ auditStatusLabel(row.status) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="reason" label="原因" min-width="260" show-overflow-tooltip />
              <el-table-column prop="detail" label="详情" min-width="260" show-overflow-tooltip />
            </el-table>
          </el-tab-pane>
        </el-tabs>
      </div>
    </el-dialog>
  </div>
</template>

<script>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from '../api'
import { ElMessage } from 'element-plus'

export default {
  setup() {
    const mirrors = ref([])
    const servers = ref([])
    const showCreateDialog = ref(false)
    const editingMirror = ref(null)
    const detailVisible = ref(false)
    const detailConfigId = ref(null)
    const detailDeprecated = ref(false)
    const detailSyncLog = ref('')
    const detailRepos = ref([])
    const auditLogs = ref([])
    const detailTab = ref('repos')
    let pollTimer = null

    const form = ref({
      source_server_id: null,
      target_server_id: null,
      sync_interval: 30,
      sync_on_commit: true,
    })

    const primaryServers = computed(() => servers.value.filter(s => s.role === 'primary'))
    const nonPrimaryServers = computed(() => servers.value.filter(s => s.role !== 'primary'))
    const detailMirror = computed(() => mirrors.value.find(m => m.id === detailConfigId.value) || null)

    function hasRunningMirrors(list = mirrors.value) {
      return list.some(m => m.status === 'syncing')
    }

    function syncPolling() {
      if (hasRunningMirrors()) {
        if (!pollTimer) {
          pollTimer = setInterval(() => load({ silent: true }), 2000)
        }
      } else if (pollTimer) {
        clearInterval(pollTimer)
        pollTimer = null
      }
    }

    function load(options = {}) {
      return Promise.all([api.get('/mirrors'), api.get('/servers')])
        .then(([mRes, sRes]) => {
          mirrors.value = mRes.data
          servers.value = sRes.data
          syncPolling()
          if (detailVisible.value && hasRunningMirrors(mirrors.value)) {
            refreshDetail()
          }
        })
    }

    function createMirror() {
      api.post('/mirrors', form.value).then(res => {
        ElMessage.success('镜像配置已创建')
        showCreateDialog.value = false
        resetForm()
        load()
        api.post(`/mirrors/${res.data.id}/setup`).then(() => {
          ElMessage.info('开始创建镜像仓库...')
          setTimeout(load, 5000)
        })
      })
    }

    function updateMirror() {
      if (!editingMirror.value) return
      api.put(`/mirrors/${editingMirror.value.id}`, {
        sync_interval: form.value.sync_interval,
        sync_on_commit: form.value.sync_on_commit,
      }).then(() => {
        ElMessage.success('已更新')
        showCreateDialog.value = false
        editingMirror.value = null
        load()
      })
    }

    function editMirror(m) {
      if (m.deprecated) {
        ElMessage.warning('旧镜像配置已弃用，请删除后重新创建 Push Mirror')
        return
      }
      editingMirror.value = m
      form.value = {
        source_server_id: m.source_server_id,
        target_server_id: m.target_server_id,
        sync_interval: m.sync_interval,
        sync_on_commit: m.sync_on_commit,
      }
      showCreateDialog.value = true
    }

    function deleteMirror(id) {
      api.delete(`/mirrors/${id}`).then(() => {
        ElMessage.success('已删除')
        load()
      })
    }

    function doSync(id) {
      api.post(`/mirrors/${id}/sync`).then(() => {
        ElMessage.info('同步已触发')
        load({ silent: true })
        setTimeout(load, 5000)
      })
    }

    function repairMirror(id) {
      api.post(`/mirrors/${id}/setup`).then(() => {
        ElMessage.info('开始刷新仓库列表并修复镜像关系...')
        load({ silent: true })
        setTimeout(load, 5000)
      })
    }

    function showDetail(m) {
      detailConfigId.value = m.id
      detailDeprecated.value = !!m.deprecated
      detailSyncLog.value = mirrorFailureLog(m)
      detailTab.value = 'repos'
      detailVisible.value = true
      refreshDetail()
    }

    function loadDetailRepo() {
      if (!detailConfigId.value) return
      api.get(`/mirrors/${detailConfigId.value}/status`).then(res => {
        detailRepos.value = res.data
      })
    }

    function loadAuditLogs() {
      if (!detailConfigId.value) return
      api.get(`/mirrors/${detailConfigId.value}/audit-logs`).then(res => {
        auditLogs.value = res.data
      })
    }

    function refreshDetail() {
      loadDetailRepo()
      loadAuditLogs()
    }

    function syncOneRepo(repo) {
      api.post(`/mirrors/${detailConfigId.value}/sync-repo/${repo.repo_name}`).then(() => {
        ElMessage.success('同步已触发')
        setTimeout(loadDetailRepo, 3000)
      })
    }

    function resetForm() {
      editingMirror.value = null
      form.value = { source_server_id: null, target_server_id: null, sync_interval: 30, sync_on_commit: true }
    }

    function fmt(d) { return d ? new Date(d).toLocaleString() : '-' }

    function mirrorFailureLog(m) {
      if (!m || !m.last_sync_log) return ''
      if (!['failed', 'partial'].includes(m.status) && m.last_sync_status !== 'failed') return ''
      return m.last_sync_log
    }

    function repoStatusType(status) {
      if (status === 'success') return 'success'
      if (status === 'failed') return 'danger'
      if (status === 'syncing') return 'primary'
      if (status === 'missing_source') return 'warning'
      return 'info'
    }

    function repoStatusLabel(status) {
      const labels = {
        success: '正常',
        failed: '失败',
        syncing: '同步中',
        missing_source: '源缺失',
        pending: '等待中',
      }
      return labels[status] || status
    }

    function auditStatusType(status) {
      if (status === 'success') return 'success'
      if (status === 'failed') return 'danger'
      if (status === 'skipped') return 'warning'
      return 'info'
    }

    function auditStatusLabel(status) {
      const labels = {
        success: '成功',
        failed: '失败',
        skipped: '跳过',
      }
      return labels[status] || status
    }

    function isMirrorRunning(m) {
      return m && m.status === 'syncing'
    }

    function mirrorProgressPct(m) {
      return Math.max(0, Math.min(Number((m && m.progress_percent) || 0), 100))
    }

    function mirrorProgressLabel(m) {
      return (m && m.progress_label) || '正在处理镜像任务'
    }

    function mirrorProgressDetail(m) {
      if (!m) return ''
      return m.progress_detail || m.current_repo_name || ''
    }

    function mirrorProgressStatus(m) {
      if (!m) return ''
      if ((m.progress_stage || '').includes('failed') || m.status === 'failed') return 'exception'
      if (m.status === 'partial') return 'warning'
      if (m.status === 'success' && mirrorProgressPct(m) >= 100) return 'success'
      return ''
    }

    function actionLabel(action) {
      const labels = {
        discover_repo: '发现仓库',
        create_target_repo: '创建目标仓库',
        rename_target_repo: '目标仓库改名',
        create_push_mirror: '创建 Push Mirror',
        update_push_mirror: '更新 Push Mirror',
        sync_push_mirror: '同步 Push Mirror',
        mark_missing_source: '标记源缺失',
        skip_missing_source: '跳过源缺失',
      }
      return labels[action] || action
    }

    onMounted(load)
    onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })

    return { mirrors, servers, showCreateDialog, editingMirror, form, detailVisible, detailRepos,
             auditLogs,
             detailTab, detailMirror,
             detailSyncLog,
             primaryServers, nonPrimaryServers,
             load, createMirror, updateMirror, editMirror, deleteMirror, doSync, repairMirror,
             showDetail, loadDetailRepo, loadAuditLogs, refreshDetail, syncOneRepo, resetForm, fmt,
             detailDeprecated, mirrorFailureLog, repoStatusType, repoStatusLabel,
             auditStatusType, auditStatusLabel, actionLabel,
             isMirrorRunning, mirrorProgressPct, mirrorProgressLabel, mirrorProgressDetail, mirrorProgressStatus }
  },
}
</script>

<style scoped>
.section-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px;
}
.section-title { font-size: 18px; font-weight: 700; color: #1a1a2e; margin: 0; }

.mirror-list { display: flex; flex-direction: column; gap: 16px; }
.mirror-card { padding: 22px; border-radius: 14px; }
.mirror-card-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;
}
.mirror-path { display: flex; align-items: center; gap: 10px; }
.mirror-server-name { font-size: 16px; font-weight: 700; color: #1a1a2e; }
.mirror-arrow { font-size: 18px; color: var(--color-primary); font-weight: 700; }
.mirror-card-info { display: flex; flex-wrap: wrap; gap: 16px; margin-bottom: 14px; }
.mirror-info-item { font-size: 13px; color: #6b7280; }
.mirror-fail { color: #ef4444 !important; font-weight: 600; }
.mirror-card-actions { display: flex; gap: 8px; }
.mirror-progress {
  margin: 0 0 14px; padding: 10px 12px; border-radius: 10px;
  background: rgba(255,255,255,0.52); border: 1px solid rgba(0,122,255,0.12);
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.76);
}
.detail-progress { margin-bottom: 16px; }
.progress-header {
  display: flex; justify-content: space-between; align-items: center; gap: 10px;
  margin-bottom: 6px;
}
.progress-label {
  min-width: 0; font-size: 12px; font-weight: 700; color: #1a1a2e;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.progress-pct { font-size: 12px; color: var(--color-primary); font-weight: 800; flex-shrink: 0; }
.progress-detail {
  margin-top: 6px; font-size: 11px; color: #667085; line-height: 1.35;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; word-break: break-all;
}
.mirror-log {
  margin: 0 0 14px; padding: 12px 14px; border-radius: 10px; max-height: 180px; overflow: auto;
  background: rgba(255,255,255,0.46); border: 1px solid rgba(239,68,68,0.16);
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.76);
  color: #7f1d1d; font-size: 12px; line-height: 1.55; white-space: pre-wrap; word-break: break-word;
}
.detail-log { max-height: 120px; }
.detail-shell { max-height: 72vh; overflow: auto; padding-right: 2px; }

.detail-header {
  display: flex; justify-content: space-between; align-items: center; margin: 4px 0 12px;
  font-weight: 600; font-size: 15px; color: #1a1a2e;
}
.detail-tabs :deep(.el-tabs__header) { margin: 0 0 14px; }
.detail-tabs :deep(.el-tabs__item) { font-weight: 650; color: #64748b; }
.detail-tabs :deep(.el-tabs__item.is-active) { color: var(--color-primary); }
.compact-table {
  border: 1px solid rgba(255,255,255,0.52);
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.72), 0 12px 28px rgba(15,23,42,0.06);
}
.compact-table :deep(.el-table__header th) {
  background: rgba(248,250,252,0.72) !important;
  color: #334155;
  font-weight: 700;
}
.compact-table :deep(.el-table__row) { height: 48px; }
.compact-table :deep(.cell) {
  line-height: 20px;
  color: #263241;
  white-space: nowrap;
}
.compact-table :deep(.el-table__row:hover > td.el-table__cell) {
  background: rgba(240,249,255,0.62) !important;
}
.status-tag {
  min-width: 56px;
  justify-content: center;
  font-weight: 700;
  letter-spacing: 0;
}
:deep(.mirror-detail-dialog .el-dialog__body) { padding-top: 16px !important; }
.icon-btn-sm {
  width: 30px; height: 30px; border-radius: 8px; border: 1px solid rgba(0,0,0,0.06);
  background: var(--glass-control); cursor: pointer; font-size: 14px; color: var(--text-secondary);
  box-shadow: inset 0 1px 0 var(--glass-highlight), var(--shadow-xs);
  transition: background 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease, color 0.18s ease, transform 0.18s ease;
  display: inline-flex; align-items: center; justify-content: center;
}
.icon-btn-sm:hover { background: var(--glass-surface-hover); border-color: rgba(0,122,255,0.18); color: var(--color-primary); transform: rotate(90deg); }
</style>
