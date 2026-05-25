<template>
  <div>
    <div class="section-header">
      <div class="header-left">
        <h3 class="section-title">定时任务</h3>
        <button class="icon-btn" @click="loadData" title="刷新">↻</button>
      </div>
      <el-button type="primary" @click="openDialog()">创建定时任务</el-button>
    </div>

    <el-alert
      v-if="criticalAlert"
      type="error"
      closable
      show-icon
      :title="criticalAlert"
      style="margin-bottom:16px"
    />

    <div class="glass-card" style="padding:0;animation:fadeInUp 0.5s ease both;">
      <el-table :data="tasks" stripe v-loading="loading" @expand-change="loadLogs" style="width:100%">
        <el-table-column type="expand">
          <template #default="{ row }">
            <div v-if="logsCache[row.id]" style="padding:0 20px 10px">
              <el-table v-if="logsCache[row.id].length" :data="flatLogs(logsCache[row.id])" :key="row.id + '-' + logsCache[row.id].length" size="small">
                <el-table-column label="时间" width="160">
                  <template #default="{ row: lr }">{{ fmt(lr.started_at) }}</template>
                </el-table-column>
                <el-table-column prop="stage" label="阶段" width="130" />
                <el-table-column label="状态" width="80">
                  <template #default="{ row: lr }">
                    <el-tag :type="statusType(lr.status)" size="small">{{ lr.status }}</el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="detail" label="详情" min-width="300" show-overflow-tooltip />
              </el-table>
              <div v-else class="log-empty">暂无执行历史</div>
            </div>
            <div v-else style="padding:10px 20px;color:#9ca3af">加载中...</div>
          </template>
        </el-table-column>
        <el-table-column prop="name" label="名称" width="160" />
        <el-table-column prop="source_server_name" label="源服务器" width="140" />
        <el-table-column label="目标服务器" min-width="200">
          <template #default="{ row }">
            <template v-if="row.target_ids && row.target_ids.length">
              <el-tag v-for="tid in row.target_ids" :key="tid" size="small" style="margin-right:4px">
                {{ serverNameMap[tid] || tid }}
              </el-tag>
            </template>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="执行时间" width="120">
          <template #default="{ row }">{{ pad(row.schedule_hour) }}:{{ pad(row.schedule_minute) }}</template>
        </el-table-column>
        <el-table-column label="启用" width="70">
          <template #default="{ row }">
            <el-switch v-model="row.enabled" size="small" @change="toggleEnabled(row)" />
          </template>
        </el-table-column>
        <el-table-column prop="last_status" label="上次" width="80">
          <template #default="{ row }">
            <el-tag v-if="row.last_status" :type="statusType(row.last_status)" size="small">{{ row.last_status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="last_run_at" label="上次执行" width="150">
          <template #default="{ row }">{{ row.last_run_at ? new Date(row.last_run_at).toLocaleString() : '-' }}</template>
        </el-table-column>
        <el-table-column label="进度" min-width="260">
          <template #default="{ row }">
            <div v-if="row.last_status === 'running'" class="schedule-progress">
              <div class="progress-header">
                <span class="progress-label">{{ scheduleProgressLabel(row) }}</span>
                <span class="progress-pct">{{ scheduleProgressPct(row) }}%</span>
              </div>
              <el-progress
                :percentage="scheduleProgressPct(row)"
                :status="scheduleProgressStatus(row)"
                :stroke-width="7"
                :show-text="false"
              />
              <div v-if="scheduleProgressDetail(row)" class="progress-detail">
                {{ scheduleProgressDetail(row) }}
              </div>
            </div>
            <span v-else class="text-muted">-</span>
          </template>
        </el-table-column>
        <el-table-column prop="last_log" label="日志" min-width="150" show-overflow-tooltip />
        <el-table-column label="操作" width="240" fixed="right">
          <template #default="{ row }">
            <el-tooltip :content="cooldownTip(row)" :disabled="!isCooldown(row)" placement="top">
              <el-button size="small" @click="runNow(row)" :disabled="isCooldown(row)">立即执行</el-button>
            </el-tooltip>
            <el-button size="small" @click="openDialog(row)">编辑</el-button>
            <el-popconfirm title="确定删除?" @confirm="deleteTask(row)">
              <template #reference><el-button size="small" type="danger">删除</el-button></template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog :title="isEdit ? '编辑定时任务' : '创建定时任务'" v-model="dialogVisible" width="550px" destroy-on-close>
      <el-form :model="form" label-width="100px">
        <el-form-item label="名称"><el-input v-model="form.name" placeholder="每日凌晨备份" /></el-form-item>
        <el-form-item label="源服务器">
          <el-select v-model="form.source_server_id" placeholder="选择主服务器" style="width:100%">
            <el-option v-for="s in primaryServers" :key="s.id" :label="`${s.name} (${s.host})`" :value="s.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="目标服务器">
          <el-select v-model="form.target_ids" multiple placeholder="选择备份服务器" style="width:100%">
            <el-option v-for="s in backupServers" :key="s.id" :label="`${s.name} (${s.host})`" :value="s.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="执行时间">
          <el-select v-model="form.schedule_hour" style="width:100px">
            <el-option v-for="h in 24" :key="h-1" :label="pad(h-1)" :value="h-1" />
          </el-select>
          <span style="margin:0 8px">时</span>
          <el-select v-model="form.schedule_minute" style="width:100px">
            <el-option v-for="m in 60" :key="m-1" :label="pad(m-1)" :value="m-1" />
          </el-select>
          <span style="margin-left:8px">分</span>
          <span style="color:#9ca3af;margin-left:12px;font-size:12px">UTC 时间</span>
        </el-form-item>
        <el-form-item label="启用"><el-switch v-model="form.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveTask" :loading="saving">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

export default {
  setup() {
    const tasks = ref([])
    const servers = ref([])
    const loading = ref(false)
    const saving = ref(false)
    const dialogVisible = ref(false)
    const isEdit = ref(false)
    const editingId = ref(null)
    const logsCache = ref({})
    const expandedTaskIds = ref(new Set())
    let pollTimer = null
    const now = ref(Date.now())
    const clockTimer = setInterval(() => { now.value = Date.now() }, 1000)

    const defaultForm = {
      name: '', source_server_id: null, target_ids: [],
      schedule_hour: 2, schedule_minute: 0, enabled: true,
    }
    const form = ref({ ...defaultForm })

    const primaryServers = computed(() => servers.value.filter(s => s.role === 'primary'))
    const backupServers = computed(() => servers.value.filter(s => s.role === 'backup'))
    const criticalAlert = computed(() => {
      const failed = tasks.value.filter(t => t.last_status === 'failed')
      if (!failed.length) return ''
      const pg = failed.some(t => t.last_log && t.last_log.includes('PostgreSQL'))
      if (pg) return '⚠️ 检测到 PostgreSQL 相关恢复失败！目标服务器可能数据损坏，请立即登录检查！'
      const names = failed.map(t => t.name).join('、')
      return `⚠️ 定时任务执行失败：${names}，请检查执行历史！`
    })
    const serverNameMap = computed(() => {
      const m = {}
      servers.value.forEach(s => { m[s.id] = s.name })
      return m
    })

    function hasRunningTasks(list = tasks.value) {
      return list.some(t => t.last_status === 'running')
    }

    function syncPolling() {
      if (hasRunningTasks()) {
        if (!pollTimer) {
          pollTimer = setInterval(() => loadData({ silent: true }), 2000)
        }
      } else if (pollTimer) {
        clearInterval(pollTimer)
        pollTimer = null
      }
    }

    function notifyFinished(prevRunning) {
      tasks.value.forEach(t => {
        if (!prevRunning.has(t.id) || t.last_status === 'running') return
        if (t.last_status === 'success') {
          ElMessage.success(`${t.name} — 执行完成`)
        } else if (t.last_status === 'failed') {
          ElMessage.error(t.last_log || `${t.name} — 执行失败`)
        }
      })
    }

    function refreshExpandedLogs() {
      expandedTaskIds.value.forEach(id => fetchLogs(id))
    }

    function loadData(options = {}) {
      const silent = Boolean(options.silent)
      const prevRunning = new Set(tasks.value.filter(t => t.last_status === 'running').map(t => t.id))
      if (!silent) loading.value = true
      Promise.all([api.get('/schedules'), api.get('/servers')]).then(([tRes, sRes]) => {
        tasks.value = tRes.data
        servers.value = sRes.data
        notifyFinished(prevRunning)
        syncPolling()
        refreshExpandedLogs()
      }).finally(() => {
        if (!silent) loading.value = false
      })
    }

    function openDialog(row) {
      if (row) {
        isEdit.value = true
        editingId.value = row.id
        form.value = { ...row, target_ids: row.target_ids || [] }
      } else {
        isEdit.value = false
        editingId.value = null
        form.value = { ...defaultForm }
      }
      dialogVisible.value = true
    }

    function saveTask() {
      saving.value = true
      const data = { ...form.value }
      const req = isEdit.value
        ? api.put(`/schedules/${editingId.value}`, data)
        : api.post('/schedules', data)
      req.then(() => {
        dialogVisible.value = false
        loadData()
      }).finally(() => { saving.value = false })
    }

    function deleteTask(row) {
      api.delete(`/schedules/${row.id}`).then(() => {
        expandedTaskIds.value.delete(row.id)
        loadData()
      })
    }

    function toggleEnabled(row) {
      api.put(`/schedules/${row.id}`, { enabled: row.enabled })
    }

    function runNow(row) {
      api.post(`/schedules/${row.id}/run`).then(() => {
        loadData({ silent: true })
      }).catch(err => {
        loadData()
        ElMessage.error(err.response?.data?.error || '请求失败')
      })
    }

    function pad(n) { return String(n).padStart(2, '0') }

    function isCooldown(row) {
      if (!row.last_run_at) return false
      return now.value - new Date(row.last_run_at).getTime() < 300000
    }

    function cooldownTip(row) {
      if (!row.last_run_at) return ''
      const s = Math.ceil((300000 - (now.value - new Date(row.last_run_at).getTime())) / 1000)
      return s > 0 ? `请等待 ${s} 秒` : ''
    }

    function statusType(status) {
      if (status === 'success') return 'success'
      if (status === 'running') return 'warning'
      return 'danger'
    }

    function scheduleProgressPct(row) {
      return Math.max(0, Math.min(Number(row.progress_percent || 0), 100))
    }

    function scheduleProgressLabel(row) {
      return row.progress_label || '正在执行定时任务'
    }

    function scheduleProgressDetail(row) {
      return row.progress_detail || ''
    }

    function scheduleProgressStatus(row) {
      if ((row.progress_stage || '').includes('failed')) return 'exception'
      if (row.last_status === 'failed') return 'exception'
      if (row.last_status === 'success' && scheduleProgressPct(row) >= 100) return 'success'
      return ''
    }

    function fetchLogs(taskId) {
      logsCache.value = { ...logsCache.value, [taskId]: logsCache.value[taskId] || [] }
      return api.get(`/schedules/${taskId}/logs`).then(res => {
        logsCache.value = { ...logsCache.value, [taskId]: res.data }
      })
    }

    function loadLogs(row, expandedRows) {
      const expanded = expandedRows.some(r => r.id === row.id)
      if (expanded) {
        expandedTaskIds.value.add(row.id)
        fetchLogs(row.id)
      } else {
        expandedTaskIds.value.delete(row.id)
      }
    }

    function fmt(d) { return d ? new Date(d).toLocaleString() : '-' }

    function flatLogs(logs) {
      const rows = []
      logs.forEach(l => {
        if (l.steps && l.steps.length) {
          l.steps.forEach(step => {
            rows.push({
              started_at: step.started_at || l.started_at,
              stage: step.target ? `${step.stage}：${step.target}` : step.stage,
              status: step.status,
              detail: step.detail || '-',
            })
          })
          return
        }
        rows.push({
          started_at: l.started_at,
          stage: '备份',
          status: l.backup_status || l.status,
          detail: l.backup_error ? `备份失败: ${l.backup_error}` : (l.log ? l.log.split(';')[0] : '-'),
        })
        const restores = l.restore_results || []
        restores.forEach(r => {
          rows.push({
            stage: '→ ' + r.target,
            status: r.status,
            detail: r.status === 'failed' ? (r.error || '') : '成功',
          })
        })
      })
      return rows
    }

    onMounted(loadData)
    onUnmounted(() => { if (pollTimer) clearInterval(pollTimer); clearInterval(clockTimer) })
    return { tasks, loading, saving, dialogVisible, isEdit, form,
             primaryServers, backupServers, serverNameMap, logsCache, criticalAlert,
             loadData, openDialog, saveTask, deleteTask, toggleEnabled, runNow,
              pad, loadLogs, fmt, flatLogs, now, isCooldown, cooldownTip, statusType,
              scheduleProgressPct, scheduleProgressLabel, scheduleProgressDetail, scheduleProgressStatus }
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
  background: rgba(255,255,255,0.5); cursor: pointer; font-size: 16px;
  transition: all 0.25s; display: flex; align-items: center; justify-content: center;
  backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
}
.icon-btn:hover { background: rgba(255,255,255,0.85); transform: rotate(90deg); }

.schedule-progress {
  padding: 8px 10px; border-radius: 10px;
  background: rgba(255,255,255,0.48); border: 1px solid rgba(0,122,255,0.12);
}
.progress-header {
  display: flex; justify-content: space-between; align-items: center; gap: 8px;
  margin-bottom: 5px;
}
.progress-label {
  min-width: 0; font-size: 12px; font-weight: 600; color: #1a1a2e;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.progress-pct { font-size: 12px; color: var(--color-primary); font-weight: 700; flex-shrink: 0; }
.progress-detail {
  margin-top: 5px; font-size: 11px; color: #6b7280; line-height: 1.35;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; word-break: break-all;
}
.log-empty { padding: 14px 0; color: #9ca3af; font-size: 12px; text-align: center; }
.text-muted { color: #d1d5db; }
</style>
