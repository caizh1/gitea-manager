<template>
  <div class="alert-bell-wrapper">
    <span v-if="reviewCount" class="alert-latest-text text-danger">{{ reviewCount }} 个 PR 需要处理</span>
    <span v-else-if="latestAlert" class="alert-latest-text" :class="{ 'text-danger': activeCount > 0 && latestAlert }">
      {{ alertLabel(latestAlert.alert_type) }}: {{ latestAlert.server_name }}
    </span>
    <span v-else class="alert-latest-text">无告警</span>
    <el-popover placement="bottom-end" :width="420" trigger="click" popper-class="alert-popover">
      <template #reference>
        <button type="button" class="bell-icon-wrap" :aria-label="`告警中心，${activeCount + reviewCount} 项未解决`">
          <el-icon :size="20" class="bell-icon"><Bell /></el-icon>
          <span v-if="activeCount + reviewCount > 0" class="bell-badge">{{ activeCount + reviewCount > 99 ? '99+' : activeCount + reviewCount }}</span>
        </button>
      </template>
      <div class="alert-panel">
        <div class="alert-panel-header">
          <span class="alert-panel-title">告警中心</span>
          <el-button v-if="resolvedCount > 0" size="small" text type="primary" @click="clearResolved">清除已解决</el-button>
        </div>
        <section v-if="reviewCount" class="review-priority">
          <strong>Review 紧急待办 · {{ reviewCount }} 个 PR</strong>
          <div v-for="item in reviewAttention.items.slice(0, 3)" :key="`${item.repository}-${item.number}`" class="review-alert-row"><span>{{ item.repo_name }} #{{ item.number }}<small>{{ item.blocked_count ? `阻塞 ${item.blocked_count} 条审查` : '发布结果待核对' }}</small></span><el-button size="small" type="primary" @click="router.push(attentionRoute(item))">处理</el-button></div>
          <el-button link type="primary" @click="router.push(attentionRoute())">查看全部待办</el-button>
        </section>
        <div v-if="reviewAttention.error" class="failure">Review 待办更新失败，保留最后数据</div>
        <div class="alert-list" v-if="alerts.length > 0">
          <div v-for="a in alerts" :key="a.id" class="alert-item" :class="'alert-' + a.status">
            <div class="alert-item-header">
              <span class="alert-type-dot" :class="a.status === 'active' ? 'dot-active' : 'dot-resolved'"></span>
              <span class="alert-item-type">{{ alertLabel(a.alert_type) }}</span>
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
        <div v-else-if="!reviewCount" class="alert-empty">暂无告警</div>
      </div>
    </el-popover>
  </div>
</template>

<script>
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { Bell } from '@element-plus/icons-vue'
import { api } from '../api'
import { useRouter } from 'vue-router'
import { useReviewAttention, attentionRoute } from '../composables/useReviewAttention'

export default {
  components: { Bell },
  setup() {
    const router = useRouter(), reviewAttention = useReviewAttention()
    const reviewCount = computed(() => reviewAttention.summary?.pr_count || 0)
    const alerts = ref([])
    const activeCount = ref(0)
    const resolvedCount = ref(0)
    const latestAlert = ref(null)
    let timer = null

    const alertLabel = type => ({ backup_failed: '备份失败', restore_failed: '恢复失败', policy_failed: '策略应用失败' }[type] || '任务失败')

    function loadSummary() {
      api.get('/alerts/summary').then(res => {
        activeCount.value = res.data.active_count || 0
        const latest = res.data.latest_alert
        latestAlert.value = latest && latest.status === 'active' ? latest : null
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

    return { router, reviewAttention, reviewCount, attentionRoute, alertLabel, alerts, activeCount, resolvedCount, latestAlert, clearOne, clearResolved, fmtTime }
  },
}
</script>

<style scoped>
.review-priority { padding: 12px; margin-bottom: 12px; border-radius: 10px; background: #fff3f2; }
.review-alert-row { display: flex; gap: 12px; align-items: center; justify-content: space-between; margin: 14px 0; }
.review-alert-row span { min-width: 0; overflow-wrap: anywhere; font-size: 13px; }
.review-alert-row small { display: block; margin-top: 6px; color: #b45349; }
.failure { color: #b45349; font-size: 12px; margin-bottom: 10px; }
.alert-bell-wrapper {
  display: flex;
  align-items: center;
  gap: 12px;
}

.alert-latest-text {
  font-size: 12px;
  color: var(--text-muted);
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
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  min-width: 36px;
  padding: 0 6px;
  flex-shrink: 0;
  height: 36px;
  border-radius: 8px;
  background: var(--glass-control);
  border: 1px solid rgba(0, 0, 0, 0.06);
  box-shadow: inset 0 1px 0 var(--glass-highlight), var(--shadow-xs);
  transition: background 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease;
}

.bell-icon-wrap:hover {
  background: var(--glass-surface-hover);
  border-color: rgba(0, 122, 255, 0.18);
}

.bell-icon {
  color: #6b7280;
  flex-shrink: 0;
  transition: color 0.2s;
}

.bell-icon-wrap:hover .bell-icon {
  color: var(--color-primary);
}

.bell-badge {
  flex-shrink: 0;
  background: var(--color-danger);
  color: #fff;
  font-size: 10px;
  font-weight: 700;
  min-width: 18px;
  height: 18px;
  line-height: 18px;
  border-radius: 9px;
  text-align: center;
  padding: 0 4px;
  box-shadow: 0 8px 18px rgba(255, 59, 48, 0.24);
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
  max-height: min(360px, calc(100dvh - 200px));
  overflow-y: auto;
}

.alert-item {
  padding: 10px 12px;
  border-radius: 8px;
  margin-bottom: 8px;
  background: rgba(0, 0, 0, 0.02);
  border: 1px solid rgba(0, 0, 0, 0.04);
  transition: background-color 0.18s ease, border-color 0.18s ease;
}

.alert-item:hover {
  background: rgba(0, 0, 0, 0.04);
}

.alert-item.alert-active {
  border-left: 3px solid #ef4444;
}

.alert-item.alert-resolved {
  border-left: 3px solid #10b981;
}

.alert-item-header {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
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
  flex-shrink: 0;
  font-size: 13px;
  font-weight: 600;
  color: #1a1a2e;
}

.alert-item-server {
  min-width: 0;
  flex: 1 1 120px;
  overflow-wrap: anywhere;
  font-size: 12px;
  color: #6b7280;
}

.resolved-tag {
  margin-left: auto;
}

.alert-item-msg {
  font-size: 12px;
  color: var(--text-secondary);
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
  color: var(--text-muted);
}

.alert-empty {
  text-align: center;
  color: var(--text-muted);
  padding: 24px 0;
  font-size: 13px;
}
</style>
