<template>
  <div>
    <div class="section-header">
      <div class="header-left">
        <el-button size="small" @click="goBack" text>
          <el-icon><ArrowLeft /></el-icon> 返回作者列表
        </el-button>
        <h3 class="section-title">{{ authorName }}</h3>
      </div>
      <div class="header-actions">
        <el-select v-model="selectedPeriod" size="small" style="width:100px" @change="loadTrend">
          <el-option label="月" value="month" />
          <el-option label="季度" value="quarter" />
          <el-option label="半年" value="half_year" />
          <el-option label="年" value="year" />
        </el-select>
      </div>
    </div>

    <div v-if="detail" class="stat-grid">
      <div class="stat-card stat-blue">
        <div class="stat-icon">📝</div>
        <div class="stat-num">{{ detail.total_commits }}</div>
        <div class="stat-label">总提交</div>
      </div>
      <div class="stat-card stat-green">
        <div class="stat-icon">➕</div>
        <div class="stat-num">{{ formatNum(detail.total_additions) }}</div>
        <div class="stat-label">新增行</div>
      </div>
      <div class="stat-card stat-orange">
        <div class="stat-icon">➖</div>
        <div class="stat-num">{{ formatNum(detail.total_deletions) }}</div>
        <div class="stat-label">删除行</div>
      </div>
      <div class="stat-card stat-purple">
        <div class="stat-icon">📦</div>
        <div class="stat-num">{{ detail.repo_count }}</div>
        <div class="stat-label">贡献仓库</div>
      </div>
    </div>

    <div v-if="detail && detail.email" class="glass-card" style="padding:14px 20px;margin-bottom:20px;display:flex;align-items:center;gap:16px;">
      <div class="detail-avatar">{{ authorName.charAt(0).toUpperCase() }}</div>
      <div>
        <div style="font-size:14px;font-weight:600;color:#1a1a2e;">{{ authorName }}</div>
        <div style="font-size:12px;color:#9ca3af;">{{ detail.email }}</div>
      </div>
      <div style="margin-left:auto;display:flex;gap:20px;">
        <div v-if="detail.first_date" style="text-align:center;">
          <div style="font-size:11px;color:#9ca3af;">首次提交</div>
          <div style="font-size:13px;font-weight:600;color:#1a1a2e;">{{ detail.first_date.slice(0, 10) }}</div>
        </div>
        <div v-if="detail.last_date" style="text-align:center;">
          <div style="font-size:11px;color:#9ca3af;">最近提交</div>
          <div style="font-size:13px;font-weight:600;color:#1a1a2e;">{{ detail.last_date.slice(0, 10) }}</div>
        </div>
      </div>
    </div>

    <div class="section-sub-header" style="margin-bottom:12px;">
      <span class="section-sub-title">提交趋势</span>
    </div>
    <div class="glass-card" style="padding:20px;margin-bottom:20px;">
      <div v-if="trendData.length === 0" style="text-align:center;color:#9ca3af;padding:40px;">暂无趋势数据</div>
      <div v-else class="chart-container" ref="trendChartRef"></div>
    </div>

    <div class="section-sub-header" style="margin-bottom:12px;">
      <span class="section-sub-title">仓库贡献明细</span>
      <el-select v-model="repoSortBy" size="small" style="width:120px" @change="loadRepos">
        <el-option label="按提交数" value="commits" />
        <el-option label="按新增行" value="additions" />
        <el-option label="按最近" value="recent" />
      </el-select>
    </div>
    <div class="glass-card" style="padding:0;">
      <el-table :data="repos" stripe style="width:100%" size="small">
        <el-table-column label="仓库" prop="repo_name" min-width="200" />
        <el-table-column label="提交数" prop="commits" width="90" align="center" sortable />
        <el-table-column label="新增行" width="100" align="center" sortable sort-by="additions">
          <template #default="{ row }">{{ formatNum(row.additions) }}</template>
        </el-table-column>
        <el-table-column label="删除行" width="100" align="center" sortable sort-by="deletions">
          <template #default="{ row }">{{ formatNum(row.deletions) }}</template>
        </el-table-column>
        <el-table-column label="占比" width="100" align="center">
          <template #default="{ row }">
            <div class="pct-bar">
              <div class="pct-fill" :style="{ width: Math.min(row.contribution_pct, 100) + '%' }"></div>
              <span class="pct-text">{{ row.contribution_pct }}%</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="最近提交" width="110">
          <template #default="{ row }">
            <span v-if="row.last_date">{{ row.last_date.slice(0, 10) }}</span>
            <span v-else style="color:#d1d5db;">-</span>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<script>
import { ref, onMounted, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import { ArrowLeft } from '@element-plus/icons-vue'

export default {
  components: { ArrowLeft },
  setup() {
    const route = useRoute()
    const router = useRouter()
    const authorName = ref(decodeURIComponent(route.params.name))
    const serverId = ref(route.query.server_id || '')
    const detail = ref(null)
    const repos = ref([])
    const trendData = ref([])
    const selectedPeriod = ref('month')
    const repoSortBy = ref('commits')
    const trendChartRef = ref(null)

    function loadDetail() {
      if (!serverId.value) return
      api.get(`/statistics/${serverId.value}/authors/${encodeURIComponent(authorName.value)}`).then(res => {
        detail.value = res.data
      }).catch(() => {})
    }

    function loadRepos() {
      if (!serverId.value) return
      api.get(`/statistics/${serverId.value}/authors/${encodeURIComponent(authorName.value)}/repos?sort_by=${repoSortBy.value}`).then(res => {
        repos.value = res.data
      }).catch(() => {})
    }

    function loadTrend() {
      if (!serverId.value) return
      api.get(`/statistics/${serverId.value}/authors/${encodeURIComponent(authorName.value)}/trend?period=${selectedPeriod.value}`).then(res => {
        trendData.value = res.data
        nextTick(renderChart)
      }).catch(() => {})
    }

    function renderChart() {
      if (!trendChartRef.value || trendData.value.length === 0) return
      const container = trendChartRef.value
      container.innerHTML = ''
      const canvas = document.createElement('canvas')
      canvas.width = container.clientWidth || 600
      canvas.height = 260
      container.appendChild(canvas)
      const ctx = canvas.getContext('2d')
      const data = trendData.value
      const maxVal = Math.max(...data.map(d => d.commits), 1)
      const w = canvas.width
      const h = canvas.height
      const padding = { top: 20, right: 20, bottom: 50, left: 60 }
      const chartW = w - padding.left - padding.right
      const chartH = h - padding.top - padding.bottom

      ctx.fillStyle = '#f8fafc'
      ctx.fillRect(0, 0, w, h)

      ctx.strokeStyle = '#e2e8f0'
      ctx.lineWidth = 1
      for (let i = 0; i <= 5; i++) {
        const y = padding.top + chartH * (1 - i / 5)
        ctx.beginPath()
        ctx.moveTo(padding.left, y)
        ctx.lineTo(w - padding.right, y)
        ctx.stroke()
        ctx.fillStyle = '#94a3b8'
        ctx.font = '11px sans-serif'
        ctx.textAlign = 'right'
        ctx.fillText(Math.round(maxVal * i / 5), padding.left - 8, y + 4)
      }

      const barW = Math.min(36, chartW / data.length - 8)
      data.forEach((d, i) => {
        const x = padding.left + (i + 0.5) * (chartW / data.length) - barW / 2
        const barH = (d.commits / maxVal) * chartH
        const y = padding.top + chartH - barH

        const gradient = ctx.createLinearGradient(x, y, x, y + barH)
        gradient.addColorStop(0, '#007aff')
        gradient.addColorStop(1, '#5ac8fa')
        ctx.fillStyle = gradient
        ctx.beginPath()
        ctx.roundRect(x, y, barW, barH, 4)
        ctx.fill()

        ctx.fillStyle = '#64748b'
        ctx.font = '10px sans-serif'
        ctx.textAlign = 'center'
        ctx.save()
        ctx.translate(x + barW / 2, h - padding.bottom + 12)
        ctx.rotate(-0.4)
        ctx.fillText(d.period_key, 0, 0)
        ctx.restore()
      })
    }

    function goBack() {
      router.push({ path: '/statistics/authors', query: { server_id: serverId.value } })
    }

    function formatNum(n) {
      if (!n) return '0'
      if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M'
      if (n >= 1000) return (n / 1000).toFixed(1) + 'K'
      return String(n)
    }

    onMounted(() => {
      loadDetail()
      loadRepos()
      loadTrend()
    })

    return { authorName, detail, repos, trendData, selectedPeriod, repoSortBy, trendChartRef, goBack, formatNum, loadRepos, loadTrend }
  },
}
</script>

<style scoped>
.section-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px;
}
.header-left { display: flex; align-items: center; gap: 8px; }
.section-title { font-size: 18px; font-weight: 700; color: #1a1a2e; margin: 0; }
.header-actions { display: flex; align-items: center; gap: 8px; }
.section-sub-header {
  display: flex; justify-content: space-between; align-items: center;
}
.section-sub-title { font-size: 15px; font-weight: 700; color: #1a1a2e; }

.stat-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 20px; }
.stat-card {
  padding: 22px 24px; border-radius: 22px; position: relative; overflow: hidden;
  color: var(--text-primary); transition: background 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease, transform 0.18s ease;
  animation: fadeInUp 0.22s ease both;
  background: var(--glass-surface);
  border: 1px solid rgba(255,255,255,0.60);
  box-shadow: inset 0 1px 0 var(--glass-highlight), var(--shadow-sm);
}
.stat-card:hover { background: var(--glass-surface-hover); border-color: rgba(255,255,255,0.70); transform: translateY(-2px); box-shadow: inset 0 1px 0 rgba(255,255,255,0.88), var(--shadow-md); }
.stat-card::before {
  content: ''; position: absolute; top: -50%; right: -30%; width: 160px; height: 160px;
  border-radius: 46% 54% 52% 48%; background: rgba(255,255,255,0.34);
}
.stat-card::after {
  content: ''; position: absolute; bottom: -30%; left: -20%; width: 120px; height: 120px;
  border-radius: 55% 45% 48% 52%; background: rgba(255,255,255,0.24);
}
.stat-blue, .stat-green, .stat-orange, .stat-purple { background: var(--glass-surface); }
.stat-icon { font-size: 28px; margin-bottom: 8px; opacity: 0.9; }
.stat-num { font-size: 28px; font-weight: 700; margin-bottom: 2px; letter-spacing: -1px; }
.stat-label { font-size: 13px; opacity: 0.85; font-weight: 500; }

.detail-avatar {
  width: 40px; height: 40px; border-radius: 50%; flex-shrink: 0;
  background: rgba(0,122,255,0.12); color: var(--color-primary);
  border: 1px solid rgba(0,122,255,0.16);
  display: flex; align-items: center; justify-content: center;
  font-size: 18px; font-weight: 700;
}

.chart-container { width: 100%; min-height: 260px; }

.pct-bar {
  position: relative; width: 100%; height: 20px; background: rgba(0,0,0,0.04);
  border-radius: 4px; overflow: hidden; display: flex; align-items: center;
}
.pct-fill {
  position: absolute; left: 0; top: 0; bottom: 0;
  background: rgba(0,122,255,0.14);
  border-radius: 4px;
}
.pct-text {
  position: relative; z-index: 1; font-size: 11px; font-weight: 600;
  color: #64748b; padding-left: 6px;
}

:deep(.el-table) { --el-table-border-color: rgba(0,0,0,0.04); }
</style>
