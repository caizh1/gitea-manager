<template>
  <div>
    <div class="section-header">
      <h3 class="section-title">服务器仪表盘</h3>
      <div class="header-actions">
        <button class="icon-btn" @click="loadData" title="刷新">↻</button>
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

    <el-row :gutter="18" class="recent-row">
      <el-col :span="12">
        <div class="glass-card recent-card">
          <div class="recent-card-header">
            <span class="recent-card-title">最近备份记录</span>
            <el-button size="small" text type="primary" @click="$router.push('/backups')">查看全部</el-button>
          </div>
          <div v-if="recentBackups.length === 0" class="recent-empty">暂无备份记录</div>
          <div v-else class="recent-list">
            <div v-for="b in recentBackups" :key="b.id" class="recent-item">
              <div class="recent-item-left">
                <span class="recent-status-icon" :class="b.status === 'success' ? 'icon-success' : 'icon-failed'">
                  {{ b.status === 'success' ? '✅' : '❌' }}
                </span>
                <div class="recent-item-info">
                  <div class="recent-item-main">
                    <span class="recent-item-name">{{ b.source_server_name }}<el-tag v-if="b.source_server_deleted" type="danger" size="small" effect="plain" class="deleted-tag">服务器已删除</el-tag></span>
                    <el-tag :type="b.status === 'success' ? 'success' : 'danger'" size="small" effect="dark">{{ b.status === 'success' ? '成功' : '失败' }}</el-tag>
                  </div>
                  <div class="recent-item-sub">
                    <span v-if="b.status === 'success'">{{ b.file_size_display }}</span>
                    <span v-else class="recent-error-msg">{{ b.error_msg.slice(0, 60) }}</span>
                    <span class="recent-time">{{ fmtShort(b.started_at) }}</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </el-col>
      <el-col :span="12">
        <div class="glass-card recent-card">
          <div class="recent-card-header">
            <span class="recent-card-title">最近恢复记录</span>
            <el-button size="small" text type="primary" @click="$router.push('/restore')">查看全部</el-button>
          </div>
          <div v-if="recentRestores.length === 0" class="recent-empty">暂无恢复记录</div>
          <div v-else class="recent-list">
            <div v-for="r in recentRestores" :key="r.id" class="recent-item">
              <div class="recent-item-left">
                <span class="recent-status-icon" :class="r.status === 'success' ? 'icon-success' : 'icon-failed'">
                  {{ r.status === 'success' ? '✅' : '❌' }}
                </span>
                <div class="recent-item-info">
                  <div class="recent-item-main">
                    <span class="recent-item-name">{{ r.target_server_name }}<el-tag v-if="r.target_server_deleted" type="danger" size="small" effect="plain" class="deleted-tag">服务器已删除</el-tag></span>
                    <el-tag :type="r.status === 'success' ? 'success' : 'danger'" size="small" effect="dark">{{ r.status === 'success' ? '成功' : '失败' }}</el-tag>
                  </div>
                  <div class="recent-item-sub">
                    <span v-if="r.status === 'success'" class="recent-backup-name">{{ r.backup_filename.slice(0, 30) }}</span>
                    <span v-else class="recent-error-msg">{{ r.error_msg.slice(0, 60) }}</span>
                    <span class="recent-time">{{ fmtShort(r.started_at) }}</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </el-col>
    </el-row>

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
import { ref, onMounted, onUnmounted } from 'vue'
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
    const recentBackups = ref([])
    const recentRestores = ref([])

    function loadData() {
      Promise.all([
        api.get('/servers'),
        api.get('/backups'),
        api.get('/restore-tasks'),
        api.get('/schedules'),
        api.get('/dashboard/recent'),
      ]).then(([sRes, bRes, rRes, tRes, dRes]) => {
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
        recentBackups.value = dRes.data.recent_backups
        recentRestores.value = dRes.data.recent_restores
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
              loadData()
            }
          })
        }, 2000)
      }).catch(() => { backingUpId.value = null })
    }

    function fmt(d) { return d ? new Date(d).toLocaleString() : '-' }
    function fmtShort(d) {
      if (!d) return ''
      const dt = new Date(d)
      return `${dt.getMonth()+1}/${dt.getDate()} ${dt.getHours().toString().padStart(2,'0')}:${dt.getMinutes().toString().padStart(2,'0')}`
    }

    onMounted(loadData)
    onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })

    return { servers, backingUpId, refreshingAll, backupCount, restoreRate, scheduleCount, failedRestoreServers, recentBackups, recentRestores, loadData, doBackup, refreshServer, refreshAll, fmt, fmtShort }
  },
}
</script>

<style scoped>
.section-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px;
}
.section-title {
  font-size: 18px; font-weight: 760; color: var(--text-primary); letter-spacing: 0; margin: 0;
}
.header-actions { display: flex; align-items: center; gap: 8px; }
.icon-btn {
  width: 34px; height: 34px; border-radius: 14px; border: 1px solid rgba(15,23,42,0.08);
  background: rgba(255,255,255,0.62); cursor: pointer; font-size: 16px;
  transition: all 0.25s; display: flex; align-items: center; justify-content: center;
  backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
}
.icon-btn:hover { background: rgba(255,255,255,0.86); color: var(--color-primary); transform: rotate(90deg); }

.stat-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }
.stat-card {
  padding: 22px 24px; border-radius: 22px; position: relative; overflow: hidden;
  color: var(--text-primary); transition: all 0.3s ease; animation: fadeInUp 0.5s ease both;
  background: rgba(255,255,255,0.62);
  backdrop-filter: blur(32px) saturate(180%); -webkit-backdrop-filter: blur(32px) saturate(180%);
  border: 1px solid rgba(255,255,255,0.72);
  box-shadow: inset 0 1px 0 rgba(255,255,255,0.86), 0 12px 32px rgba(15,23,42,0.08);
}
.stat-card:hover { transform: translateY(-3px); box-shadow: inset 0 1px 0 rgba(255,255,255,0.86), 0 18px 46px rgba(15,23,42,0.11); }
.stat-card::before {
  content: ''; position: absolute; top: -48%; right: -28%; width: 160px; height: 160px;
  border-radius: 46% 54% 52% 48%; background: rgba(255,255,255,0.34);
  box-shadow: inset 18px 22px 48px rgba(255,255,255,0.6), inset -14px -18px 42px rgba(148,163,184,0.10);
}
.stat-card::after {
  content: ''; position: absolute; bottom: -38%; left: -18%; width: 120px; height: 120px;
  border-radius: 55% 45% 48% 52%; background: rgba(255,255,255,0.24);
}
.stat-blue { animation-delay: 0s; }
.stat-green { animation-delay: 0.06s; }
.stat-orange { animation-delay: 0.12s; }
.stat-purple { animation-delay: 0.18s; }
.stat-icon { font-size: 28px; margin-bottom: 8px; opacity: 0.78; color: var(--color-primary); }
.stat-num { font-size: 32px; font-weight: 700; margin-bottom: 2px; letter-spacing: -1px; }
.stat-label { font-size: 13px; color: var(--text-secondary); font-weight: 600; }

.recent-row { margin-bottom: 16px; }
.recent-card {
  padding: 20px; border-radius: 14px;
}
.recent-card-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;
}
.recent-card-title {
  font-size: 15px; font-weight: 700; color: var(--text-primary);
}
.recent-empty {
  text-align: center; color: #9ca3af; padding: 20px 0; font-size: 13px;
}
.recent-list {
  display: flex; flex-direction: column; gap: 10px;
}
.recent-item {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 12px; border-radius: 10px;
  background: rgba(255,255,255,0.44); border: 1px solid rgba(15,23,42,0.06);
  transition: all 0.2s;
}
.recent-item:hover {
  background: rgba(255,255,255,0.66);
}
.recent-item-left {
  display: flex; align-items: flex-start; gap: 10px; flex: 1; min-width: 0;
}
.recent-status-icon {
  font-size: 16px; margin-top: 2px; flex-shrink: 0;
}
.recent-item-info {
  flex: 1; min-width: 0;
}
.recent-item-main {
  display: flex; align-items: center; gap: 8px; margin-bottom: 4px;
}
.recent-item-name {
  font-size: 13px; font-weight: 600; color: var(--text-primary);
}
.recent-item-sub {
  display: flex; align-items: center; gap: 8px; font-size: 12px; color: #9ca3af;
}
.recent-error-msg {
  color: #ef4444; font-size: 12px;
}
.recent-backup-name {
  color: #9ca3af; font-size: 12px;
}
.recent-time {
  margin-left: auto; flex-shrink: 0; font-size: 11px; color: #d1d5db;
}
.deleted-tag {
  margin-left: 6px; font-size: 10px; vertical-align: middle;
}

.server-card {
  margin-bottom: 18px; padding: 22px; position: relative;
  animation: fadeInUp 0.5s ease both;
}
.server-card:hover { transform: translateY(-2px); }
.status-bar {
  position: absolute; left: 0; top: 0; bottom: 0; width: 4px; border-radius: 4px 0 0 4px;
}
.bar-online {
  background: var(--color-success);
  box-shadow: 0 0 14px rgba(52,199,89,0.24);
}
.bar-offline { background: var(--color-warning); }
.bar-unknown { background: var(--text-muted); }
.server-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;
}
.server-name {
  font-size: 16px; font-weight: 700; color: var(--text-primary); cursor: pointer; transition: color 0.2s;
}
.server-name:hover { color: var(--color-primary); }
.server-info p { margin: 5px 0; font-size: 13px; color: var(--text-secondary); display: flex; align-items: center; gap: 6px; }
.info-label-inline {
  display: inline-block; min-width: 36px; font-size: 11px; font-weight: 600;
  color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px;
}
.disk-warn { color: #f59e0b !important; }
.error-warn { color: #ef4444 !important; font-weight: 600; }
.check-time { color: #9ca3af !important; font-size: 12px !important; }
.server-actions {
  display: flex; gap: 8px; margin-top: 16px; padding-top: 14px;
  border-top: 1px solid rgba(15,23,42,0.06);
}
.ghost-btn {
  padding: 7px 16px; border-radius: 14px; font-size: 12px; font-weight: 600;
  cursor: pointer; transition: all 0.2s; border: none; font-family: inherit;
  background: rgba(255,255,255,0.62); color: var(--text-secondary); border: 1px solid rgba(15,23,42,0.08);
}
.ghost-btn:hover { background: rgba(255,255,255,0.86); color: var(--text-primary); }
</style>
