<template>
  <div>
    <div class="section-header">
      <h3 class="section-title">恢复操作</h3>
    </div>

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
          <el-select v-model="selectedBackupId" placeholder="选择已完成的备份" style="width:100%">
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

      <div v-if="restoring || progressStage" class="restore-progress">
        <div class="progress-header">
          <span class="progress-label">{{ progressLabel }}</span>
          <span class="progress-pct">{{ progressPct }}%</span>
        </div>
        <el-progress :percentage="progressPct" :status="progressStatus" :stroke-width="8" />
      </div>
    </div>

    <div class="glass-card" style="padding:24px;">
      <div class="card-section-header">
        <span class="card-section-title">恢复历史</span>
        <button class="icon-btn-sm" @click="load" title="刷新">↻</button>
      </div>
      <el-table :data="tasks" stripe style="width:100%">
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column prop="backup_filename" label="备份文件" min-width="250" />
        <el-table-column prop="target_server_name" label="目标服务器" width="150" />
        <el-table-column prop="status" label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ row.status }}</el-tag>
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
        <el-table-column prop="error_msg" label="错误信息" min-width="200" />
        <el-table-column prop="started_at" label="开始时间" width="170">
          <template #default="{ row }">{{ fmt(row.started_at) }}</template>
        </el-table-column>
        <el-table-column prop="completed_at" label="完成时间" width="170">
          <template #default="{ row }">{{ fmt(row.completed_at) }}</template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="verifyDialogVisible" title="恢复验证结果" width="600px">
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
            <div class="verify-mismatch-info">
              备份: {{ m.backup_commit_count }} commits | 恢复后: {{ m.target_commit_count }} commits
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
          ✅ 所有仓库 commit ID 完全匹配，恢复验证通过！
        </div>
      </div>
      <div v-else>暂无验证数据</div>
    </el-dialog>
  </div>
</template>

<script>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from '../api'

const PROGRESS_STAGES = [
  { label: '准备恢复', pct: 5 },
  { label: '上传备份数据', pct: 20 },
  { label: '停止 Gitea 服务', pct: 30 },
  { label: '覆盖数据文件', pct: 50 },
  { label: '恢复数据库', pct: 75 },
  { label: '启动 Gitea 服务', pct: 90 },
  { label: '验证 Commit ID', pct: 100 },
]

export default {
  setup() {
    const backups = ref([])
    const servers = ref([])
    const tasks = ref([])
    const restoring = ref(false)
    const selectedBackupId = ref(null)
    const selectedTargetId = ref(null)
    let pollTimer = null
    const progressStage = ref(0)
    const verifyDialogVisible = ref(false)
    const verifyData = ref(null)

    const successBackups = computed(() => backups.value.filter(b => b.status === 'success'))
    const backupServers = computed(() => servers.value.filter(s => s.role === 'backup'))

    const criticalAlert = computed(() => {
      const failed = tasks.value.filter(t => t.status === 'failed' && t.error_msg && t.error_msg.includes('PostgreSQL'))
      if (!failed.length) return ''
      return '⚠️ 检测到 PostgreSQL 相关恢复失败！目标服务器可能数据损坏，请立即登录检查！'
    })

    const progressPct = computed(() => PROGRESS_STAGES[progressStage.value]?.pct || 0)
    const progressLabel = computed(() => PROGRESS_STAGES[progressStage.value]?.label || '')
    const progressStatus = computed(() => {
      if (progressStage.value >= PROGRESS_STAGES.length - 1) return 'success'
      return ''
    })

    function load() {
      Promise.all([api.get('/backups'), api.get('/servers'), api.get('/restore-tasks')])
        .then(([bRes, sRes, tRes]) => {
          backups.value = bRes.data
          servers.value = sRes.data
          tasks.value = tRes.data
        })
    }

    function doRestore() {
      restoring.value = true
      progressStage.value = 0
      let stageIdx = 0
      const stageTimer = setInterval(() => {
        stageIdx++
        if (stageIdx < PROGRESS_STAGES.length - 1) {
          progressStage.value = stageIdx
        }
      }, 8000)

      api.post('/restore', {
        backup_id: selectedBackupId.value,
        target_server_id: selectedTargetId.value,
      }).then(() => {
        selectedBackupId.value = null
        selectedTargetId.value = null
        load()
        pollTimer = setInterval(() => {
          api.get('/restore-tasks').then(res => {
            tasks.value = res.data
            const running = tasks.value.some(t => t.status === 'running')
            if (!running) {
              clearInterval(pollTimer)
              clearInterval(stageTimer)
              pollTimer = null
              const lastTask = tasks.value[0]
              if (lastTask && lastTask.status === 'success') {
                progressStage.value = PROGRESS_STAGES.length - 2
                const verifyPoll = setInterval(() => {
                  api.get(`/restore-tasks/${lastTask.id}/verification`).then(vRes => {
                    if (vRes.data.status === 'success' || vRes.data.status === 'failed') {
                      clearInterval(verifyPoll)
                      progressStage.value = PROGRESS_STAGES.length - 1
                      setTimeout(() => {
                        restoring.value = false
                        progressStage.value = 0
                        load()
                      }, 2000)
                    }
                  })
                }, 3000)
              } else {
                restoring.value = false
                progressStage.value = 0
              }
            }
          })
        }, 2000)
      }).catch(() => {
        restoring.value = false
        progressStage.value = 0
        clearInterval(stageTimer)
      })
    }

    function triggerVerify(taskId) {
      api.post(`/restore-tasks/${taskId}/verify`).then(() => {
        const verifyPoll = setInterval(() => {
          api.get(`/restore-tasks/${taskId}/verification`).then(vRes => {
            if (vRes.data.status === 'success' || vRes.data.status === 'failed') {
              clearInterval(verifyPoll)
              load()
            }
          })
        }, 3000)
      })
    }

    function showVerify(task) {
      api.get(`/restore-tasks/${task.id}/verification`).then(res => {
        verifyData.value = res.data
        verifyDialogVisible.value = true
      })
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

    function fmt(d) { return d ? new Date(d).toLocaleString() : '-' }

    onMounted(load)
    onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })

    return { backups, servers, tasks, restoring,
             selectedBackupId, selectedTargetId,
             successBackups, backupServers, criticalAlert,
             doRestore, load, formatSize, statusType, fmt,
             progressStage, progressPct, progressLabel, progressStatus,
             verifyDialogVisible, verifyData, triggerVerify, showVerify }
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
  background: rgba(255,255,255,0.5); cursor: pointer; font-size: 14px;
  transition: all 0.25s; display: flex; align-items: center; justify-content: center;
}
.icon-btn-sm:hover { background: rgba(255,255,255,0.85); transform: rotate(90deg); }

.restore-progress {
  margin-top: 16px; padding: 16px; border-radius: 10px;
  background: rgba(102,126,234,0.04); border: 1px solid rgba(102,126,234,0.1);
}
.progress-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;
}
.progress-label { font-size: 13px; font-weight: 600; color: #1a1a2e; }
.progress-pct { font-size: 13px; color: #667eea; font-weight: 700; }

.v-ok { color: #10b981; cursor: pointer; font-weight: 600; }
.v-fail { color: #ef4444; cursor: pointer; font-weight: 600; }
.v-running { color: #f59e0b; }
.v-pending { color: #667eea; cursor: pointer; font-weight: 600; }
.v-pending:hover { text-decoration: underline; }

.verify-summary {
  display: flex; gap: 24px; margin-bottom: 20px; padding: 16px;
  background: rgba(0,0,0,0.02); border-radius: 10px;
}
.verify-stat { text-align: center; }
.verify-stat-label { display: block; font-size: 12px; color: #9ca3af; margin-bottom: 4px; }
.verify-stat-num { font-size: 24px; font-weight: 700; color: #1a1a2e; }

.verify-mismatches { margin-top: 12px; }
.verify-mismatch-title { font-size: 14px; font-weight: 600; color: #ef4444; margin-bottom: 10px; }
.verify-mismatch-item {
  padding: 10px 12px; border-radius: 8px; margin-bottom: 8px;
  background: rgba(239,68,68,0.04); border: 1px solid rgba(239,68,68,0.1);
}
.verify-mismatch-repo { font-size: 13px; font-weight: 600; color: #1a1a2e; margin-bottom: 4px; }
.verify-mismatch-info { font-size: 12px; color: #6b7280; }
.verify-mismatch-detail { font-size: 11px; color: #9ca3af; font-family: monospace; margin-top: 2px; }
.verify-all-ok { text-align: center; padding: 20px; font-size: 15px; color: #10b981; font-weight: 600; }
</style>
