<template>
  <div>
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:16px">
      <h3 style="margin:0">服务器仪表盘</h3>
      <el-button circle size="small" @click="loadServers" title="刷新">↻</el-button>
      <el-button size="small" @click="refreshAll" :loading="refreshingAll">刷新所有服务器</el-button>
    </div>

    <el-row :gutter="16" style="margin-bottom:20px">
      <el-col :span="6">
        <div class="stat-card stat-online">
          <div class="stat-num">{{ servers.filter(s => s.status === 'online').length }}/{{ servers.length }}</div>
          <div class="stat-label">在线服务器</div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-card stat-backup">
          <div class="stat-num">{{ backupCount }}</div>
          <div class="stat-label">备份总数</div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-card stat-restore">
          <div class="stat-num">{{ restoreRate }}%</div>
          <div class="stat-label">恢复成功率</div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-card stat-schedule">
          <div class="stat-num">{{ scheduleCount }}</div>
          <div class="stat-label">定时任务</div>
        </div>
      </el-col>
    </el-row>

    <el-row :gutter="20">
      <el-col :span="8" v-for="s in servers" :key="s.id">
        <el-card class="server-card" shadow="hover">
          <div class="card-left-bar" :class="'bar-' + s.status"></div>
          <div class="card-header">
            <span class="card-title" @click="$router.push('/servers/' + s.id)">{{ s.name }}</span>
            <el-tag :type="s.status === 'online' ? 'success' : 'danger'" size="small" effect="dark">{{ s.status }}</el-tag>
          </div>
          <div class="card-info">
            <p>地址: {{ s.host }}:{{ s.gitea_port }}</p>
            <p>角色: <el-tag :type="s.role === 'primary' ? '' : 'info'" size="small">{{ s.role }}</el-tag>
              <el-tag v-if="s.is_local" type="success" size="small" style="margin-left:4px">本地</el-tag>
              <el-tag v-else size="small" style="margin-left:4px">远程</el-tag>
            </p>
            <p v-if="s.version">版本: {{ s.version }}</p>
            <p>仓库: {{ s.repo_count }}  用户: {{ s.user_count }}</p>
            <p v-if="s.disk_usage" style="color:#e6a23c">磁盘: {{ s.disk_usage }}</p>
            <p v-if="failedRestoreServers[s.id]" style="color:#f56c6c;font-weight:bold">⚠️ 最近恢复失败 — 请检查</p>
            <p v-if="s.last_check_at">上次检查: {{ fmt(s.last_check_at) }}</p>
          </div>
          <div class="card-actions">
            <el-button
              v-if="s.role === 'primary'"
              type="primary"
              size="small"
              :disabled="backingUpId === s.id"
              :loading="backingUpId === s.id"
              @click="doBackup(s)"
            >
              {{ backingUpId === s.id ? '备份中...' : '立即备份' }}
            </el-button>
            <el-button
              v-if="s.role === 'backup'"
              type="success"
              size="small"
              @click="$router.push('/restore')"
            >
              恢复到此处
            </el-button>
            <el-button size="small" @click="refreshServer(s)">刷新信息</el-button>
          </div>
        </el-card>
      </el-col>
    </el-row>
    <el-empty v-if="servers.length === 0" description="暂无服务器，请先添加">
      <el-button type="primary" @click="$router.push('/servers')">添加服务器</el-button>
    </el-empty>
  </div>
</template>

<script>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from '../api'

export default {
  setup() {
    const servers = ref([])
    const backingUpId = ref(null)
    const refreshingAll = ref(false)
    let pollTimer = null
    const backupCount = ref(0)
    const restoreRate = ref('--')
    const scheduleCount = ref(0)
    const failedRestoreServers = ref({})

    function loadServers() {
      Promise.all([
        api.get('/servers'),
        api.get('/backups'),
        api.get('/restore-tasks'),
        api.get('/schedules'),
      ]).then(([sRes, bRes, rRes, tRes]) => {
        servers.value = sRes.data
        backupCount.value = bRes.data.length
        const tasks = rRes.data
        if (tasks.length) {
          const success = tasks.filter(t => t.status === 'success').length
          restoreRate.value = Math.round(success / tasks.length * 100)
        }
        scheduleCount.value = tRes.data.filter(t => t.enabled).length
        const failedMap = {}
        tasks.forEach(t => {
          if (t.status === 'failed' && t.error_msg && t.error_msg.includes('PostgreSQL')) {
            failedMap[t.target_server_id] = true
          }
        })
        failedRestoreServers.value = failedMap
      })
    }

    function refreshServer(s) {
      api.post(`/servers/${s.id}/refresh`).then(res => {
        const idx = servers.value.findIndex(i => i.id === s.id)
        if (idx >= 0) servers.value[idx] = res.data
      })
    }

    function refreshAll() {
      refreshingAll.value = true
      Promise.all(servers.value.map(s =>
        api.post(`/servers/${s.id}/refresh`).then(res => {
          const idx = servers.value.findIndex(i => i.id === s.id)
          if (idx >= 0) servers.value[idx] = res.data
        }).catch(() => {})
      )).finally(() => { refreshingAll.value = false })
    }

    function doBackup(s) {
      backingUpId.value = s.id
      api.post('/backups', { source_server_id: s.id }).then(() => {
        pollTimer = setInterval(() => {
          api.get('/backups').then(res => {
            const running = res.data.find(b => b.source_server_id === s.id && b.status === 'running')
            if (!running) {
              backingUpId.value = null
              if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
            }
          })
        }, 2000)
      }).catch(() => { backingUpId.value = null })
    }

    function fmt(d) { return d ? new Date(d).toLocaleString() : '-' }

    onMounted(loadServers)
    onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })

    return { servers, backingUpId, refreshingAll, backupCount, restoreRate, scheduleCount, failedRestoreServers, loadServers, doBackup, refreshServer, refreshAll, fmt }
  },
}
</script>

<style scoped>
.server-card { margin-bottom: 20px; border-radius: 10px; overflow: hidden; position: relative; transition: transform .2s, box-shadow .2s; }
.server-card:hover { transform: translateY(-2px); box-shadow: 0 4px 16px rgba(0,0,0,0.1); }
.card-left-bar { position: absolute; left: 0; top: 0; bottom: 0; width: 4px; }
.bar-online { background: #67c23a; }
.bar-offline { background: #f56c6c; }
.bar-unknown { background: #e6a23c; }
.card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.card-title { font-size: 16px; font-weight: 700; color: #409eff; cursor: pointer; }
.card-title:hover { text-decoration: underline; }
.card-info p { margin: 5px 0; font-size: 13px; color: #666; }
.card-actions { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }

.stat-card { border-radius: 10px; padding: 18px 20px; color: #fff; transition: transform .2s; }
.stat-card:hover { transform: translateY(-2px); }
.stat-online { background: linear-gradient(135deg, #409eff, #337ecc); }
.stat-backup { background: linear-gradient(135deg, #67c23a, #529b2e); }
.stat-restore { background: linear-gradient(135deg, #e6a23c, #cf9236); }
.stat-schedule { background: linear-gradient(135deg, #909399, #73767a); }
.stat-num { font-size: 28px; font-weight: 700; margin-bottom: 4px; }
.stat-label { font-size: 13px; opacity: 0.9; }
</style>
