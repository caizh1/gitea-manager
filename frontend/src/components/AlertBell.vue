<template>
  <div class="alert-bell-wrapper">
    <span v-if="latestAlert" class="alert-latest-text" :class="{ 'text-danger': activeCount > 0 }">
      {{ latestAlert.alert_type === 'backup_failed' ? '备份失败' : '恢复失败' }}: {{ latestAlert.server_name }}
    </span>
    <span v-else class="alert-latest-text">无告警</span>
    <el-popover placement="bottom-end" :width="420" trigger="click" popper-class="alert-popover">
      <template #reference>
        <div class="bell-icon-wrap">
          <el-icon :size="20" class="bell-icon"><Bell /></el-icon>
          <span v-if="activeCount > 0" class="bell-badge">{{ activeCount > 99 ? '99+' : activeCount }}</span>
        </div>
      </template>
      <div class="alert-panel">
        <div class="alert-panel-header">
          <span class="alert-panel-title">告警中心</span>
          <el-button v-if="resolvedCount > 0" size="small" text type="primary" @click="clearResolved">清除已解决</el-button>
        </div>
        <div class="alert-list" v-if="alerts.length > 0">
          <div v-for="a in alerts" :key="a.id" class="alert-item" :class="'alert-' + a.status">
            <div class="alert-item-header">
              <span class="alert-type-dot" :class="a.status === 'active' ? 'dot-active' : 'dot-resolved'"></span>
              <span class="alert-item-type">{{ a.alert_type === 'backup_failed' ? '备份失败' : '恢复失败' }}</span>
              <span class="alert-item-server">— {{ a.server_name }}</span>
              <el-tag v-if="a.status === 'resolved'" type="success" size="small" effect="plain" class="resolved-tag">已解决</el-tag>
            </div>
            <div class="alert-item-msg">{{ a.message.slice(0, 120) }}</div>
            <div class="alert-item-footer">
              <span class="alert-time">{{ fmtTime(a.created_at) }}</span>
              <el-button v-if="a.status === 'resolved'" size="small" text type="primary" @click="clearOne(a.id)">清除</el-button>
            </div>
          </div>
        </div>
        <div v-else class="alert-empty">暂无告警</div>
      </div>
    </el-popover>
  </div>
</template>

<script>
import { ref, onMounted, onUnmounted } from 'vue'
import { Bell } from '@element-plus/icons-vue'
import { api } from '../api'

export default {
  components: { Bell },
  setup() {
    const alerts = ref([])
    const activeCount = ref(0)
    const resolvedCount = ref(0)
    const latestAlert = ref(null)
    let timer = null

    function loadSummary() {
      api.get('/alerts/summary').then(res => {
        activeCount.value = res.data.active_count
        latestAlert.value = res.data.latest_alert
      }).catch(() => {})
    }

    function loadAlerts() {
      api.get('/alerts').then(res => {
        alerts.value = res.data
        resolvedCount.value = res.data.filter(a => a.status === 'resolved').length
      }).catch(() => {})
    }

    function clearOne(id) {
      api.post(`/alerts/${id}/clear`).then(() => {
        loadAlerts()
        loadSummary()
      })
    }

    function clearResolved() {
      api.post('/alerts/clear-resolved').then(() => {
        loadAlerts()
        loadSummary()
      })
    }

    function fmtTime(d) {
      if (!d) return ''
      return new Date(d).toLocaleString()
    }

    onMounted(() => {
      loadSummary()
      loadAlerts()
      timer = setInterval(() => {
        loadSummary()
        loadAlerts()
      }, 10000)
    })

    onUnmounted(() => {
      if (timer) clearInterval(timer)
    })

    return { alerts, activeCount, resolvedCount, latestAlert, clearOne, clearResolved, fmtTime }
  },
}
</script>

<style scoped>
.alert-bell-wrapper {
  display: flex;
  align-items: center;
  gap: 12px;
}

.alert-latest-text {
  font-size: 12px;
  color: #9ca3af;
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.alert-latest-text.text-danger {
  color: #ef4444;
  font-weight: 600;
}

.bell-icon-wrap {
  position: relative;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.5);
  border: 1px solid rgba(0, 0, 0, 0.06);
  transition: all 0.25s;
}

.bell-icon-wrap:hover {
  background: rgba(255, 255, 255, 0.85);
}

.bell-icon {
  color: #6b7280;
  transition: color 0.2s;
}

.bell-icon-wrap:hover .bell-icon {
  color: #667eea;
}

.bell-badge {
  position: absolute;
  top: -4px;
  right: -4px;
  background: linear-gradient(135deg, #ef4444, #f97316);
  color: #fff;
  font-size: 10px;
  font-weight: 700;
  min-width: 18px;
  height: 18px;
  line-height: 18px;
  border-radius: 9px;
  text-align: center;
  padding: 0 4px;
  box-shadow: 0 2px 8px rgba(239, 68, 68, 0.4);
}

.alert-panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
  padding-bottom: 10px;
  border-bottom: 1px solid rgba(0, 0, 0, 0.06);
}

.alert-panel-title {
  font-size: 15px;
  font-weight: 700;
  color: #1a1a2e;
}

.alert-list {
  max-height: 360px;
  overflow-y: auto;
}

.alert-item {
  padding: 10px 12px;
  border-radius: 8px;
  margin-bottom: 8px;
  background: rgba(0, 0, 0, 0.02);
  border: 1px solid rgba(0, 0, 0, 0.04);
  transition: all 0.2s;
}

.alert-item:hover {
  background: rgba(0, 0, 0, 0.04);
}

.alert-item.alert-active {
  border-left: 3px solid #ef4444;
}

.alert-item.alert-resolved {
  border-left: 3px solid #10b981;
  opacity: 0.75;
}

.alert-item-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}

.alert-type-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.dot-active {
  background: #ef4444;
  box-shadow: 0 0 6px rgba(239, 68, 68, 0.5);
}

.dot-resolved {
  background: #10b981;
}

.alert-item-type {
  font-size: 13px;
  font-weight: 600;
  color: #1a1a2e;
}

.alert-item-server {
  font-size: 12px;
  color: #6b7280;
}

.resolved-tag {
  margin-left: auto;
}

.alert-item-msg {
  font-size: 12px;
  color: #9ca3af;
  margin-bottom: 4px;
  word-break: break-all;
  line-height: 1.4;
}

.alert-item-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.alert-time {
  font-size: 11px;
  color: #d1d5db;
}

.alert-empty {
  text-align: center;
  color: #9ca3af;
  padding: 24px 0;
  font-size: 13px;
}
</style>
