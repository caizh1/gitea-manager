<template>
  <div>
    <div class="section-header">
      <div class="header-left">
        <h3 class="section-title">备份管理</h3>
        <button class="icon-btn" @click="load" title="刷新" aria-label="刷新">
          <el-icon><Refresh /></el-icon>
        </button>
      </div>
      <div class="header-left">
        <el-upload :show-file-list="false" accept=".zip" :before-upload="importBackup">
          <el-button :loading="importing">导入灾备 ZIP</el-button>
        </el-upload>
        <el-button type="primary" @click="showCreateDialog = true" :disabled="primaryServers.length === 0">创建备份</el-button>
      </div>
    </div>

    <el-alert v-if="pollError" :title="pollError" type="warning" :closable="false" />
    <div v-if="backups.length === 0">
      <el-empty description="暂无备份记录" />
    </div>

    <div v-else class="glass-card" style="padding:0;animation:fadeInUp 0.5s ease both;">
      <el-table :data="backups" stripe v-loading="loading" style="width:100%" max-height="var(--table-max-height)">
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column label="来源服务器" width="180">
          <template #default="{ row }">
            {{ row.source_server_name }}
            <el-tag v-if="row.source_server_deleted" type="danger" size="small" effect="plain" style="margin-left:4px;font-size:10px">服务器已删除</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态与进度" min-width="300">
          <template #default="{ row }"><OperationProgress :task="row" kind="backup" /></template>
        </el-table-column>
        <el-table-column prop="filename" label="文件名" min-width="280" />
        <el-table-column label="大小" width="100">
          <template #default="{ row }">{{ formatSize(row.file_size) }}</template>
        </el-table-column>
        <el-table-column label="Commit 快照" width="150">
          <template #default="{ row }">
            <el-tag :type="snapshotType(row)" size="small" effect="plain">
              {{ snapshotText(row) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="错误原因" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">
            <template v-if="row.status === 'failed' && row.error_msg">
              <span class="error-summary">{{ shortError(row.error_msg) }}</span>
              <el-button size="small" type="danger" text @click="showError(row)">查看</el-button>
            </template>
            <span v-else class="text-muted">-</span>
          </template>
        </el-table-column>
        <el-table-column prop="started_at" label="开始时间" width="170">
          <template #default="{ row }">{{ fmt(row.started_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="170" fixed="right">
          <template #default="{ row }">
            <div class="backup-actions" role="group" :aria-label="`备份 ${row.id} 操作`">
              <el-tooltip v-if="row.status === 'success'" content="下载 ZIP" placement="top" :show-after="200" :hide-after="0" :trigger="['hover', 'focus']" :trigger-keys="[]">
                <el-button class="backup-action" size="small" type="primary" aria-label="下载 ZIP" @click="downloadBackup(row)">
                  <Download :size="16" aria-hidden="true" />
                </el-button>
              </el-tooltip>
              <el-tooltip v-if="row.status === 'success'" :content="exporting === row.id ? '正在生成完整灾备包' : '完整灾备包'" placement="top" :show-after="200" :hide-after="0" :trigger="['hover', 'focus']" :trigger-keys="[]">
                <el-button class="backup-action" size="small" :loading="exporting === row.id" :aria-busy="exporting === row.id" aria-label="完整灾备包" @click="exportBundle(row)">
                  <Archive v-if="exporting !== row.id" :size="16" aria-hidden="true" />
                </el-button>
              </el-tooltip>
              <el-tooltip v-if="row.diagnostic_id" content="诊断包" placement="top" :show-after="200" :hide-after="0" :trigger="['hover', 'focus']" :trigger-keys="[]">
                <a class="backup-action backup-action-link" :href="`/api/management/diagnostics/operation/${row.diagnostic_id}`" aria-label="诊断包">
                  <FileHeart :size="16" aria-hidden="true" />
                </a>
              </el-tooltip>
              <el-popconfirm title="确定删除此备份?" @confirm="deleteBackup(row)">
                <template #reference>
                  <span class="backup-action-trigger">
                    <el-tooltip content="删除" placement="top" :show-after="200" :hide-after="0" :trigger="['hover', 'focus']" :trigger-keys="[]">
                      <el-button class="backup-action" size="small" type="danger" plain aria-label="删除">
                        <Trash2 :size="16" aria-hidden="true" />
                      </el-button>
                    </el-tooltip>
                  </span>
                </template>
              </el-popconfirm>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog append-to-body title="创建备份" v-model="showCreateDialog" width="450px">
      <el-form label-width="100px">
        <el-form-item label="源服务器">
          <el-select v-model="selectedServerId" placeholder="选择主服务器" style="width:100%">
            <el-option v-for="s in primaryServers" :key="s.id" :label="`${s.name} (${s.host}:${s.gitea_port})`" :value="s.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreateDialog = false">取消</el-button>
        <el-button type="primary" @click="createBackup" :loading="creating">开始备份</el-button>
      </template>
    </el-dialog>

    <el-dialog append-to-body title="备份失败原因" v-model="errorDialogVisible" width="720px">
      <div v-if="selectedErrorBackup" class="error-dialog">
        <div class="error-meta">
          <span>{{ selectedErrorBackup.filename }}</span>
          <span>{{ fmt(selectedErrorBackup.started_at) }}</span>
        </div>
        <pre class="error-content">{{ selectedErrorBackup.error_msg }}</pre>
      </div>
    </el-dialog>
  </div>
</template>

<script>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Download, Archive, FileHeart, Trash2 } from '@lucide/vue'
import { api } from '../api'
import { notifyApiError } from '../api/errors.js'
import OperationProgress from '../components/OperationProgress.vue'
import { useOperationPolling } from '../composables/useOperationPolling'
import { operationActive, utcTime } from '../components/operationDisplay'

export default {
  components: { OperationProgress, Download, Archive, FileHeart, Trash2 },
  setup() {
    const backups = ref([])
    const servers = ref([])
    const loading = ref(false)
    const creating = ref(false)
    const importing = ref(false)
    const exporting = ref(null)
    const showCreateDialog = ref(false)
    const selectedServerId = ref(null)
    const errorDialogVisible = ref(false)
    const selectedErrorBackup = ref(null)

    const primaryServers = ref([])

    const { refresh: load, error: pollError } = useOperationPolling(
      config => Promise.all([api.get('/backups', config), api.get('/servers', config)]),
      ([bRes, sRes]) => { backups.value = bRes.data; servers.value = sRes.data; primaryServers.value = sRes.data.filter(s => s.role === 'primary') },
      () => backups.value.some(operationActive),
    )

    function createBackup() {
      if (!selectedServerId.value) return
      creating.value = true
      api.post('/backups', { source_server_id: selectedServerId.value }).then(() => {
        showCreateDialog.value = false
        selectedServerId.value = null
        load()
      }).finally(() => { creating.value = false })
    }

    async function exportBundle(row) {
      exporting.value = row.id
      try {
        const result = await api.post(`/backups/${row.id}/disaster-bundle`, {}, { timeout: 0 })
        window.location.assign(result.data.download_url)
      } catch (e) { notifyApiError(e, '灾备包导出失败，请查看服务状态与磁盘空间') }
      finally { exporting.value = null }
    }
    function downloadBackup(row) {
      window.open(`/api/backups/${row.id}/download`, '_blank')
    }

    async function importBackup(file) {
      importing.value = true
      try {
        const data = new FormData()
        data.append('file', file)
        await api.post('/backups/import', data)
        await load()
      } catch (e) { notifyApiError(e, '灾备导入失败') }
      finally { importing.value = false }
      return false
    }

    function deleteBackup(row) {
      api.delete(`/backups/${row.id}`).then(() => { load() })
    }

    function shortError(message) {
      if (!message) return ''
      const firstLine = message.split('\n').find(Boolean) || message
      return firstLine.length > 90 ? firstLine.slice(0, 90) + '...' : firstLine
    }

    function showError(row) {
      selectedErrorBackup.value = row
      errorDialogVisible.value = true
    }

    function formatSize(bytes) {
      if (!bytes) return '-'
      if (bytes < 1024) return bytes + ' B'
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
      return (bytes / (1024 * 1024)).toFixed(1) + ' MB'
    }

    function statusType(s) {
      if (s === 'success') return 'success'
      if (s === 'running') return 'warning'
      return 'danger'
    }

    function snapshotText(row) {
      const status = row.commit_snapshot_status
      if (status === 'success') return `${row.commit_snapshot_repo_count || 0} 仓库`
      if (status === 'running') return '采集中'
      if (status === 'pending') return '待采集'
      if (status === 'failed') return '采集失败'
      if (row.status === 'success') return '旧备份缺失'
      return '-'
    }

    function snapshotType(row) {
      const status = row.commit_snapshot_status
      if (status === 'success') return 'success'
      if (status === 'running' || status === 'pending') return 'warning'
      if (status === 'failed' || row.status === 'success') return 'danger'
      return 'info'
    }

    function fmt(d) { return d ? new Date(utcTime(d)).toLocaleString() : '-' }


    return { backups, pollError, loading, creating, importing, importBackup, exporting, exportBundle, showCreateDialog, selectedServerId, primaryServers,
             errorDialogVisible, selectedErrorBackup,
             load, createBackup, downloadBackup, deleteBackup, shortError, showError,
             formatSize, statusType, snapshotText, snapshotType, fmt }
  },
}
</script>

<style scoped>
.section-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px;
}
.header-left { display: flex; align-items: center; gap: 8px; }
.section-title { font-size: 18px; font-weight: 700; color: #1a1a2e; margin: 0; }
.icon-btn {
  width: 34px; height: 34px; border-radius: 8px; border: 1px solid rgba(0,0,0,0.06);
  background: var(--glass-control); cursor: pointer; font-size: 16px; color: var(--text-secondary);
  box-shadow: inset 0 1px 0 var(--glass-highlight), var(--shadow-xs);
  transition: background 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease, color 0.18s ease, transform 0.18s ease;
  display: inline-flex; align-items: center; justify-content: center;
}
.icon-btn:hover { background: var(--glass-surface-hover); border-color: rgba(0,122,255,0.18); color: var(--color-primary); transform: rotate(90deg); }
.backup-actions {
  display: flex; align-items: center; flex-wrap: wrap; gap: 6px; min-width: 0; padding: 3px 0;
}
.backup-action-trigger { display: inline-flex; flex: 0 0 32px; min-width: 0; }
.backup-actions .backup-action {
  display: inline-flex; align-items: center; justify-content: center; flex: 0 0 32px;
  box-sizing: border-box; width: 32px; height: 32px; min-width: 0; padding: 0; margin: 0;
}
.backup-action :deep(svg) { flex-shrink: 0; }
.backup-action :deep(.el-icon) { margin: 0; }
.backup-action-link {
  border: 1px solid var(--el-border-color); border-radius: var(--radius-xs);
  background: var(--el-button-bg-color, var(--el-fill-color-blank)); color: var(--el-text-color-regular);
  text-decoration: none; cursor: pointer; transition: background-color 0.18s ease, border-color 0.18s ease, color 0.18s ease;
}
.backup-action-link:hover {
  color: var(--el-color-primary); background: var(--el-color-primary-light-9); border-color: var(--el-color-primary-light-7);
}
.backup-action:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
.error-summary { color: #ef4444; font-size: 12px; vertical-align: middle; }
.text-muted { color: var(--text-muted); }
.error-dialog { display: flex; flex-direction: column; gap: 12px; }
.error-meta {
  display: flex; justify-content: space-between; gap: 12px; font-size: 12px; color: #6b7280;
}
.error-content {
  margin: 0; padding: 14px; border-radius: 10px; max-height: 420px; overflow: auto;
  background: rgba(239,68,68,0.04); border: 1px solid rgba(239,68,68,0.12);
  color: #1a1a2e; font-size: 12px; line-height: 1.55; white-space: pre-wrap;
}
</style>
