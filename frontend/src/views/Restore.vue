<template>
  <div>
    <div class="section-header">
      <h3 class="section-title">恢复操作</h3>
    </div>

    <el-alert v-if="pollError" :title="pollError" type="warning" :closable="false" />
    <el-alert
      v-if="criticalAlert"
      type="error"
      :closable="false"
      show-icon
      :title="criticalAlert"
      style="margin-bottom:16px"
    />

    <div class="glass-card" style="padding:24px;margin-bottom:20px;">
      <div class="card-section-title" style="margin-bottom:16px;">执行恢复</div>
      <el-alert type="warning" :closable="false" show-icon title="警告：恢复操作将覆盖目标服务器的所有数据！请谨慎操作。" style="margin-bottom:16px" />

      <el-form label-width="110px">
        <el-form-item label="选择备份">
          <el-select v-model="selectedBackupId" placeholder="选择已完成且 Commit 快照通过的备份" style="width:100%">
            <el-option v-for="b in successBackups" :key="b.id"
              :label="`${b.filename} (${formatSize(b.file_size)} | ${fmt(b.started_at)})`"
              :value="b.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="目标服务器">
          <el-select v-model="selectedTargetId" placeholder="选择要恢复到的服务器" style="width:100%">
            <el-option v-for="s in backupServers" :key="s.id"
              :label="`${s.name} (${s.host}:${s.gitea_port})`"
              :value="s.id" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-popconfirm title="确认恢复? 此操作不可逆!"
            confirm-button-text="确认恢复" cancel-button-text="取消"
            @confirm="doRestore">
            <template #reference>
              <el-button type="danger" :disabled="!selectedBackupId || !selectedTargetId" :loading="restoring">确认恢复</el-button>
            </template>
          </el-popconfirm>
        </el-form-item>
      </el-form>

    </div>

    <div class="glass-card" style="padding:24px;">
      <div class="card-section-header">
        <span class="card-section-title">恢复历史</span>
        <button class="icon-btn-sm" @click="load" title="刷新" aria-label="刷新">
          <el-icon><Refresh /></el-icon>
        </button>
      </div>
      <el-table :data="tasks" stripe style="width:100%" max-height="var(--table-max-height)">
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column label="状态与进度" min-width="300">
          <template #default="{ row }"><OperationProgress :task="row" kind="restore" /></template>
        </el-table-column>
        <el-table-column prop="backup_filename" label="备份文件" min-width="250" />
        <el-table-column label="目标服务器" width="180">
          <template #default="{ row }">
            {{ row.target_server_name }}
            <el-tag v-if="row.target_server_deleted" type="danger" size="small" effect="plain" style="margin-left:4px;font-size:10px">服务器已删除</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="验证" width="100">
          <template #default="{ row }">
            <span v-if="row.status === 'running'">—</span>
            <span v-else-if="row.verification_status === 'success'" class="v-ok" @click="showVerify(row)">✅ 通过</span>
            <span v-else-if="row.verification_status === 'failed'" class="v-fail" @click="showVerify(row)">❌ 失败</span>
            <span v-else-if="row.verification_status === 'running'" class="v-running">⏳ 验证中</span>
            <span v-else-if="row.status === 'success'" class="v-pending" @click="triggerVerify(row.id)">验证</span>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column label="诊断"><template #default="{ row }"><a v-if="row.diagnostic_id" :href="`/api/management/diagnostics/operation/${row.diagnostic_id}`">下载诊断包</a></template></el-table-column>
        <el-table-column prop="error_msg" label="错误信息" min-width="200" show-overflow-tooltip />
        <el-table-column prop="started_at" label="开始时间" width="170">
          <template #default="{ row }">{{ fmt(row.started_at) }}</template>
        </el-table-column>
        <el-table-column prop="completed_at" label="完成时间" width="170">
          <template #default="{ row }">{{ fmt(row.completed_at) }}</template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog append-to-body v-model="verifyDialogVisible" title="恢复验证结果" width="600px">
      <div v-if="verifyData">
        <div class="verify-summary">
          <div class="verify-stat">
            <span class="verify-stat-label">总仓库数</span>
            <span class="verify-stat-num">{{ verifyData.total_repos }}</span>
          </div>
          <div class="verify-stat">
            <span class="verify-stat-label">匹配</span>
            <span class="verify-stat-num v-ok">{{ verifyData.matched_repos }}</span>
          </div>
          <div class="verify-stat">
            <span class="verify-stat-label">不匹配</span>
            <span class="verify-stat-num v-fail">{{ verifyData.mismatch_repos }}</span>
          </div>
        </div>
        <div v-if="verifyData.mismatch_details && verifyData.mismatch_details.length > 0" class="verify-mismatches">
          <div class="verify-mismatch-title">不匹配仓库详情</div>
          <div v-for="m in verifyData.mismatch_details" :key="m.repo" class="verify-mismatch-item">
            <div class="verify-mismatch-repo">❌ {{ m.repo }}</div>
            <div v-if="m.message" class="verify-mismatch-info">
              {{ m.message }}
            </div>
            <div v-if="m.path" class="verify-mismatch-detail">
              API: {{ m.path }}
            </div>
            <div v-if="m.backup_commit_count !== undefined || m.target_commit_count !== undefined" class="verify-mismatch-info">
              备份: {{ displayCount(m.backup_commit_count) }} commits | 恢复后: {{ displayCount(m.target_commit_count) }} commits
            </div>
            <div v-if="m.missing_samples && m.missing_samples.length" class="verify-mismatch-detail">
              缺少: {{ m.missing_samples.join(', ') }}{{ m.missing_count > 5 ? ` ...等${m.missing_count}个` : '' }}
            </div>
            <div v-if="m.extra_samples && m.extra_samples.length" class="verify-mismatch-detail">
              多出: {{ m.extra_samples.join(', ') }}{{ m.extra_count > 5 ? ` ...等${m.extra_count}个` : '' }}
            </div>
          </div>
        </div>
        <div v-else class="verify-all-ok">
          ✅ 健康检查通过，所有仓库 commit ID 完全匹配！
        </div>
      </div>
      <div v-else>暂无验证数据</div>
    </el-dialog>

  </div>
</template>

<script>
import { ref, computed } from 'vue'
import { api } from '../api'
import { ElMessage } from 'element-plus'
import OperationProgress from '../components/OperationProgress.vue'
import { useOperationPolling } from '../composables/useOperationPolling'
import { operationActive, utcTime } from '../components/operationDisplay'
export default {
  components: { OperationProgress },
  setup() {
    const backups = ref([]), servers = ref([]), tasks = ref([]), restoring = ref(false)
    const selectedBackupId = ref(null), selectedTargetId = ref(null)
    const verifyDialogVisible = ref(false), verifyData = ref(null)
    const successBackups = computed(() => backups.value.filter(b => b.status === 'success' && b.commit_snapshot_status === 'success'))
    const backupServers = computed(() => servers.value.filter(s => s.role === 'backup'))
    const criticalAlert = computed(() => tasks.value.some(t => t.status === 'failed' && t.error_msg?.includes('PostgreSQL')) ? '检测到 PostgreSQL 相关恢复失败，请查看执行详情核对目标状态。' : '')
    const { refresh: load, error: pollError } = useOperationPolling(
      config => Promise.all([api.get('/backups', config), api.get('/servers', config), api.get('/restore-tasks', config)]),
      ([b, s, t]) => { backups.value = b.data; servers.value = s.data; tasks.value = t.data },
      () => tasks.value.some(operationActive),
    )
    async function doRestore() {
      if (restoring.value) return
      restoring.value = true
      try {
        await api.post('/restore', { backup_id: selectedBackupId.value, target_server_id: selectedTargetId.value })
        selectedBackupId.value = null; selectedTargetId.value = null
        await load()
      } catch (e) { ElMessage.error(e.response?.data?.error || '创建恢复任务失败，请刷新确认任务状态') }
      finally { restoring.value = false }
    }
    async function triggerVerify(id) {
      try { await api.post(`/restore-tasks/${id}/verify`); await load() }
      catch (e) { ElMessage.error(e.response?.data?.error || '验证请求失败，请刷新确认任务状态') }
    }
    async function showVerify(task) {
      try { verifyData.value = (await api.get(`/restore-tasks/${task.id}/verification`)).data; verifyDialogVisible.value = true }
      catch (e) { ElMessage.error('验证结果读取失败') }
    }
    function formatSize(bytes) { return bytes ? `${(bytes / 1048576).toFixed(1)} MB` : '—' }
    const fmt = d => d ? new Date(utcTime(d)).toLocaleString() : '—'
    const displayCount = n => n ?? '—'
    return { backups, servers, tasks, restoring, selectedBackupId, selectedTargetId, successBackups, backupServers, criticalAlert,
      load, pollError, doRestore, triggerVerify, showVerify, verifyDialogVisible, verifyData, formatSize, fmt, displayCount }
  },
}
</script>

<style scoped>
.section-header { margin-bottom: 18px; }
.section-title { font-size: 18px; font-weight: 700; color: #1a1a2e; margin: 0; }
.card-section-title { font-weight: 600; font-size: 15px; color: #1a1a2e; }
.card-section-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;
}
.icon-btn-sm {
  width: 30px; height: 30px; border-radius: 8px; border: 1px solid rgba(0,0,0,0.06);
  background: var(--glass-control); cursor: pointer; font-size: 14px; color: var(--text-secondary);
  box-shadow: inset 0 1px 0 var(--glass-highlight), var(--shadow-xs);
  transition: background 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease, color 0.18s ease, transform 0.18s ease;
  display: inline-flex; align-items: center; justify-content: center;
}
.icon-btn-sm:hover { background: var(--glass-surface-hover); border-color: rgba(0,122,255,0.18); color: var(--color-primary); transform: rotate(90deg); }

.restore-progress {
  margin-top: 16px; padding: 16px; border-radius: 10px;
  background: rgba(255,255,255,0.48); border: 1px solid rgba(0,122,255,0.12);
}
.progress-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;
}
.progress-label { font-size: 13px; font-weight: 600; color: #1a1a2e; }
.progress-pct { font-size: 13px; color: var(--color-primary); font-weight: 700; }
.progress-detail { margin-top: 8px; font-size: 12px; color: #6b7280; word-break: break-all; }

.v-ok { color: #10b981; cursor: pointer; font-weight: 600; }
.v-fail { color: #ef4444; cursor: pointer; font-weight: 600; }
.v-running { color: #f59e0b; }
.v-pending { color: var(--color-primary); cursor: pointer; font-weight: 600; }
.v-pending:hover { text-decoration: underline; }

.verify-summary {
  display: flex; gap: 24px; margin-bottom: 20px; padding: 16px;
  background: rgba(0,0,0,0.02); border-radius: 10px;
}
.verify-stat { text-align: center; }
.verify-stat-label { display: block; font-size: 12px; color: var(--text-muted); margin-bottom: 4px; }
.verify-stat-num { font-size: 24px; font-weight: 700; color: #1a1a2e; }

.verify-mismatches { margin-top: 12px; }
.verify-mismatch-title { font-size: 14px; font-weight: 600; color: #ef4444; margin-bottom: 10px; }
.verify-mismatch-item {
  padding: 10px 12px; border-radius: 8px; margin-bottom: 8px;
  background: rgba(239,68,68,0.04); border: 1px solid rgba(239,68,68,0.1);
}
.verify-mismatch-repo { font-size: 13px; font-weight: 600; color: #1a1a2e; margin-bottom: 4px; }
.verify-mismatch-info { font-size: 12px; color: #6b7280; }
.verify-mismatch-detail { font-size: 11px; color: var(--text-muted); font-family: monospace; margin-top: 2px; }
.verify-all-ok { text-align: center; padding: 20px; font-size: 15px; color: #10b981; font-weight: 600; }

.step-dialog-header {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px;
}
.step-dialog-title { color: #1a1a2e; font-size: 15px; font-weight: 700; }
.step-dialog-subtitle { color: #6b7280; font-size: 12px; line-height: 1.5; margin-top: 4px; }
.step-list { display: grid; gap: 12px; min-height: 80px; }
.step-card {
  padding: 14px; border: 1px solid rgba(0,0,0,0.08); border-radius: 10px;
  background: rgba(255,255,255,0.48); min-width: 0;
}
.step-card-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.step-title-group { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
.step-label { color: #1a1a2e; font-size: 14px; font-weight: 600; }
.step-key { color: var(--text-muted); font-family: monospace; font-size: 11px; }
.step-elapsed { color: #6b7280; font-size: 12px; flex: 0 0 auto; }
.step-detail { color: #4b5563; font-size: 12px; line-height: 1.6; margin-top: 8px; word-break: break-word; }
.step-meta { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; color: var(--text-muted); font-size: 11px; margin-top: 8px; }
.step-path { color: #6b7280; font-family: monospace; font-size: 11px; margin-top: 8px; word-break: break-all; }
.metric-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 8px; margin-top: 10px; }
.metric-item { display: flex; flex-direction: column; gap: 2px; padding: 8px 10px; border-radius: 7px; background: rgba(0,0,0,0.025); min-width: 0; }
.metric-key { color: var(--text-muted); font-size: 10px; }
.metric-value { color: #374151; font-size: 12px; word-break: break-word; }
.step-logs { margin-top: 10px; color: var(--color-primary); font-size: 12px; }
.step-logs summary { cursor: pointer; }
.log-block { margin-top: 8px; }
.log-title { color: #6b7280; font-weight: 600; margin-bottom: 4px; }
.log-block pre {
  max-height: 240px; overflow: auto; margin: 0; padding: 10px; border-radius: 7px;
  background: #111827; color: #d1fae5; font-size: 11px; line-height: 1.5; white-space: pre-wrap; word-break: break-all;
}
.log-error pre { color: #fecaca; }

@media (max-width: 640px) {
  .step-dialog-header { align-items: stretch; flex-direction: column; }
  .step-card-header { align-items: flex-start; }
}
</style>
