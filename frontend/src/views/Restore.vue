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
        <el-table-column prop="error_msg" label="错误信息" min-width="200" />
        <el-table-column prop="started_at" label="开始时间" width="170">
          <template #default="{ row }">{{ fmt(row.started_at) }}</template>
        </el-table-column>
        <el-table-column prop="completed_at" label="完成时间" width="170">
          <template #default="{ row }">{{ fmt(row.completed_at) }}</template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from '../api'

export default {
  setup() {
    const backups = ref([])
    const servers = ref([])
    const tasks = ref([])
    const restoring = ref(false)
    const selectedBackupId = ref(null)
    const selectedTargetId = ref(null)
    let pollTimer = null

    const successBackups = computed(() => backups.value.filter(b => b.status === 'success'))
    const backupServers = computed(() => servers.value.filter(s => s.role === 'backup'))

    const criticalAlert = computed(() => {
      const failed = tasks.value.filter(t => t.status === 'failed' && t.error_msg && t.error_msg.includes('PostgreSQL'))
      if (!failed.length) return ''
      return '⚠️ 检测到 PostgreSQL 相关恢复失败！目标服务器可能数据损坏，请立即登录检查！'
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
            if (!running && pollTimer) {
              clearInterval(pollTimer)
              pollTimer = null
            }
          })
        }, 2000)
      }).finally(() => { restoring.value = false })
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
             doRestore, load, formatSize, statusType, fmt }
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
</style>
