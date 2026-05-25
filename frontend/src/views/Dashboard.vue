<template>
  <div>
    <div class="section-header">
      <h3 class="section-title">服务器仪表盘</h3>
      <div class="header-actions">
        <button class="icon-btn" @click="loadServers" title="刷新">↻</button>
        <el-button size="small" @click="refreshAll" :loading="refreshingAll">刷新所有服务器</el-button>
      </div>
    </div>

    <div class="stat-grid">
      <div class="stat-card stat-blue">
        <div class="stat-icon">🖥️</div>
        <div class="stat-num">{{ servers.filter(s => s.status === 'online').length }}/{{ servers.length }}</div>
        <div class="stat-label">在线服务器</div>
      </div>
      <div class="stat-card stat-green">
        <div class="stat-icon">💾</div>
        <div class="stat-num">{{ backupCount }}</div>
        <div class="stat-label">备份总数</div>
      </div>
      <div class="stat-card stat-orange">
        <div class="stat-icon">✅</div>
        <div class="stat-num">{{ restoreRate }}%</div>
        <div class="stat-label">恢复成功率</div>
      </div>
      <div class="stat-card stat-purple">
        <div class="stat-icon">⏰</div>
        <div class="stat-num">{{ scheduleCount }}</div>
        <div class="stat-label">定时任务</div>
      </div>
    </div>

    <div class="section-header" style="margin-top:8px">
      <h3 class="section-title" style="font-size:16px;">服务器概览</h3>
    </div>

    <el-row :gutter="18">
      <el-col :span="8" v-for="(s, idx) in servers" :key="s.id">
        <div class="glass-card server-card" :style="{ animationDelay: idx * 0.06 + 's' }">
          <div class="status-bar" :class="'bar-' + s.status"></div>
          <div class="server-header">
            <span class="server-name" @click="$router.push('/servers/' + s.id)">{{ s.name }}</span>
            <el-tag :type="s.status === 'online' ? 'success' : 'danger'" size="small" effect="dark">{{ s.status }}</el-tag>
          </div>
          <div class="server-info">
            <p><span class="info-label-inline">地址</span> {{ s.host }}:{{ s.gitea_port }}</p>
            <p>
              <span class="info-label-inline">角色</span>
              <el-tag :type="s.role === 'primary' ? 'warning' : 'info'" size="small">{{ s.role }}</el-tag>
              <el-tag v-if="s.is_local" type="success" size="small" style="margin-left:4px">本地</el-tag>
              <el-tag v-else size="small" style="margin-left:4px">远程</el-tag>
            </p>
            <p v-if="s.version"><span class="info-label-inline">版本</span> {{ s.version }}</p>
            <p><span class="info-label-inline">数据</span> 仓库 {{ s.repo_count }} &nbsp; 用户 {{ s.user_count }}</p>
            <p v-if="s.disk_usage" class="disk-warn"><span class="info-label-inline">磁盘</span> {{ s.disk_usage }}</p>
            <p v-if="failedRestoreServers[s.id]" class="error-warn">⚠️ 最近恢复失败 — 请检查</p>
            <p v-if="s.last_check_at" class="check-time">上次检查: {{ fmt(s.last_check_at) }}</p>
          </div>
          <div class="server-actions">
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
            <button class="ghost-btn" @click="refreshServer(s)">刷新信息</button>
          </div>
        </div>
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
.section-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px;
}
.section-title {
  font-size: 18px; font-weight: 700; color: #1a1a2e; letter-spacing: -0.3px; margin: 0;
}
.header-actions { display: flex; align-items: center; gap: 8px; }
.icon-btn {
  width: 34px; height: 34px; border-radius: 8px; border: 1px solid rgba(0,0,0,0.06);
  background: rgba(255,255,255,0.5); cursor: pointer; font-size: 16px;
  transition: all 0.25s; display: flex; align-items: center; justify-content: center;
  backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
}
.icon-btn:hover { background: rgba(255,255,255,0.85); transform: rotate(90deg); }

.stat-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }
.stat-card {
  padding: 22px 24px; border-radius: 16px; position: relative; overflow: hidden;
  color: #fff; transition: all 0.3s ease; animation: fadeInUp 0.5s ease both;
}
.stat-card:hover { transform: translateY(-3px); box-shadow: 0 12px 36px rgba(0,0,0,0.15); }
.stat-card::before {
  content: ''; position: absolute; top: -50%; right: -30%; width: 160px; height: 160px;
  border-radius: 50%; background: rgba(255,255,255,0.1);
}
.stat-card::after {
  content: ''; position: absolute; bottom: -30%; left: -20%; width: 120px; height: 120px;
  border-radius: 50%; background: rgba(255,255,255,0.06);
}
.stat-blue { background: linear-gradient(135deg, #89f7fe 0%, #66a6ff 100%); animation-delay: 0s; }
.stat-green { background: linear-gradient(135deg, #43e97b 0%, #38f9d7 100%); animation-delay: 0.06s; }
.stat-orange { background: linear-gradient(135deg, #fa709a 0%, #fee140 100%); animation-delay: 0.12s; }
.stat-purple { background: linear-gradient(135deg, #a18cd1 0%, #fbc2eb 100%); animation-delay: 0.18s; }
.stat-icon { font-size: 28px; margin-bottom: 8px; opacity: 0.9; }
.stat-num { font-size: 32px; font-weight: 700; margin-bottom: 2px; letter-spacing: -1px; }
.stat-label { font-size: 13px; opacity: 0.85; font-weight: 500; }

.server-card {
  margin-bottom: 18px; padding: 22px; position: relative;
  animation: fadeInUp 0.5s ease both;
}
.server-card:hover { transform: translateY(-2px); }
.status-bar {
  position: absolute; left: 0; top: 0; bottom: 0; width: 4px; border-radius: 4px 0 0 4px;
}
.bar-online {
  background: linear-gradient(135deg, #43e97b, #38f9d7);
  box-shadow: 0 0 12px rgba(67,233,123,0.4);
}
.bar-offline { background: linear-gradient(135deg, #fa709a, #fee140); }
.bar-unknown { background: linear-gradient(135deg, #a18cd1, #fbc2eb); }
.server-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;
}
.server-name {
  font-size: 16px; font-weight: 700; color: #1a1a2e; cursor: pointer; transition: color 0.2s;
}
.server-name:hover { color: #667eea; }
.server-info p { margin: 5px 0; font-size: 13px; color: #6b7280; display: flex; align-items: center; gap: 6px; }
.info-label-inline {
  display: inline-block; min-width: 36px; font-size: 11px; font-weight: 600;
  color: #9ca3af; text-transform: uppercase; letter-spacing: 0.5px;
}
.disk-warn { color: #f59e0b !important; }
.error-warn { color: #ef4444 !important; font-weight: 600; }
.check-time { color: #9ca3af !important; font-size: 12px !important; }
.server-actions {
  display: flex; gap: 8px; margin-top: 16px; padding-top: 14px;
  border-top: 1px solid rgba(0,0,0,0.04);
}
.ghost-btn {
  padding: 7px 16px; border-radius: 8px; font-size: 12px; font-weight: 600;
  cursor: pointer; transition: all 0.2s; border: none; font-family: inherit;
  background: rgba(0,0,0,0.03); color: #6b7280; border: 1px solid rgba(0,0,0,0.06);
}
.ghost-btn:hover { background: rgba(0,0,0,0.06); color: #1a1a2e; }
</style>
