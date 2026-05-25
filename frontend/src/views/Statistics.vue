<template>
  <div>
    <div class="section-header">
      <h3 class="section-title">统计分析</h3>
      <div class="header-actions">
        <el-select v-model="selectedServerId" placeholder="选择服务器" size="small" style="width:200px">
          <el-option v-for="s in primaryServers" :key="s.id" :label="s.name" :value="s.id" />
        </el-select>
        <el-button size="small" type="primary" @click="refreshData" :loading="refreshing" :disabled="!selectedServerId">刷新数据</el-button>
      </div>
    </div>

    <div v-if="!selectedServerId" class="glass-card" style="padding:40px;text-align:center;">
      <el-empty description="请选择一个服务器查看统计" />
    </div>

    <div v-else>
      <div class="stat-grid">
        <div class="stat-card stat-blue">
          <div class="stat-icon">📝</div>
          <div class="stat-num">{{ overview.total_commits }}</div>
          <div class="stat-label">总提交</div>
        </div>
        <div class="stat-card stat-green">
          <div class="stat-icon">💻</div>
          <div class="stat-num">{{ formatNum(overview.total_code_lines) }}</div>
          <div class="stat-label">代码行数</div>
        </div>
        <div class="stat-card stat-orange">
          <div class="stat-icon">📄</div>
          <div class="stat-num">{{ formatNum(overview.total_doc_lines) }}</div>
          <div class="stat-label">文档行数</div>
        </div>
        <div class="stat-card stat-purple">
          <div class="stat-icon">📦</div>
          <div class="stat-num">{{ overview.total_repos }}</div>
          <div class="stat-label">仓库数</div>
        </div>
      </div>

      <div class="section-sub-header">
        <span class="section-sub-title">Commit 趋势</span>
        <div class="period-tabs">
          <button v-for="p in periods" :key="p.value" class="period-btn" :class="{ active: selectedPeriod === p.value }" @click="changePeriod(p.value)">{{ p.label }}</button>
        </div>
      </div>
      <div class="glass-card" style="padding:20px;margin-bottom:20px;">
        <div v-if="commitTrend.length === 0" style="text-align:center;color:#9ca3af;padding:40px;">暂无数据，请先刷新采集</div>
        <div v-else class="chart-container" ref="trendChartRef"></div>
      </div>

      <el-row :gutter="18" style="margin-bottom:20px;">
        <el-col :span="12">
          <div class="glass-card" style="padding:20px;">
            <div class="section-sub-title" style="margin-bottom:14px;">仓库排名 (Top 10)</div>
            <div class="rank-list">
              <div v-for="(r, i) in repoRanking" :key="r.repo_name" class="rank-item">
                <span class="rank-index">{{ i + 1 }}</span>
                <span class="rank-name">{{ r.repo_name }}</span>
                <span class="rank-value">{{ r.commit_count }} commits</span>
                <div class="rank-bar" :style="{ width: barWidth(r.commit_count, repoRanking) }"></div>
              </div>
              <div v-if="repoRanking.length === 0" class="rank-empty">暂无数据</div>
            </div>
          </div>
        </el-col>
        <el-col :span="12">
          <div class="glass-card" style="padding:20px;">
            <div class="section-sub-title" style="margin-bottom:14px;">作者贡献排名 (Top 10)</div>
            <div class="rank-list">
              <div v-for="(a, i) in authorRanking" :key="a.name" class="rank-item">
                <span class="rank-index">{{ i + 1 }}</span>
                <span class="rank-name">{{ a.name }}</span>
                <span class="rank-value">{{ a.commits }} commits</span>
                <div class="rank-bar" :style="{ width: barWidth(a.commits, authorRanking) }"></div>
              </div>
              <div v-if="authorRanking.length === 0" class="rank-empty">暂无数据</div>
            </div>
          </div>
        </el-col>
      </el-row>

      <div class="glass-card" style="padding:20px;margin-bottom:20px;">
        <div class="section-sub-title" style="margin-bottom:14px;">代码 vs 文档占比</div>
        <div class="ratio-bar">
          <div class="ratio-segment ratio-code" :style="{ width: codePct + '%' }">
            <span v-if="codePct > 15">代码 {{ codePct }}%</span>
          </div>
          <div class="ratio-segment ratio-doc" :style="{ width: docPct + '%' }">
            <span v-if="docPct > 10">文档 {{ docPct }}%</span>
          </div>
          <div class="ratio-segment ratio-other" :style="{ width: otherPct + '%' }">
            <span v-if="otherPct > 8">其他 {{ otherPct }}%</span>
          </div>
        </div>
      </div>

      <div class="glass-card" style="padding:20px;">
        <div class="section-sub-title" style="margin-bottom:14px;">语言分布 (Top 10)</div>
        <div class="lang-list">
          <div v-for="lang in overview.top_languages" :key="lang.name" class="lang-item">
            <span class="lang-name">{{ lang.name }}</span>
            <div class="lang-bar-wrap">
              <div class="lang-bar" :style="{ width: langBarWidth(lang.lines) }"></div>
            </div>
            <span class="lang-lines">{{ formatNum(lang.lines) }} 行</span>
          </div>
          <div v-if="overview.top_languages.length === 0" class="rank-empty">暂无数据</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, computed, onMounted, watch, nextTick } from 'vue'
import { api } from '../api'
import { ElMessage } from 'element-plus'

export default {
  setup() {
    const servers = ref([])
    const selectedServerId = ref(null)
    const refreshing = ref(false)
    const overview = ref({ total_commits: 0, total_code_lines: 0, total_doc_lines: 0, total_other_lines: 0, total_repos: 0, top_languages: [] })
    const commitTrend = ref([])
    const repoRanking = ref([])
    const authorRanking = ref([])
    const selectedPeriod = ref('month')
    const trendChartRef = ref(null)
    let chartInstance = null

    const periods = [
      { label: '月', value: 'month' },
      { label: '季度', value: 'quarter' },
      { label: '半年', value: 'half_year' },
      { label: '年', value: 'year' },
    ]

    const primaryServers = computed(() => servers.value.filter(s => s.role === 'primary'))

    const codePct = computed(() => {
      const total = overview.value.total_code_lines + overview.value.total_doc_lines + overview.value.total_other_lines
      return total ? Math.round(overview.value.total_code_lines / total * 100) : 0
    })
    const docPct = computed(() => {
      const total = overview.value.total_code_lines + overview.value.total_doc_lines + overview.value.total_other_lines
      return total ? Math.round(overview.value.total_doc_lines / total * 100) : 0
    })
    const otherPct = computed(() => {
      const total = overview.value.total_code_lines + overview.value.total_doc_lines + overview.value.total_other_lines
      return total ? Math.round(overview.value.total_other_lines / total * 100) : 0
    })

    function loadData() {
      if (!selectedServerId.value) return
      Promise.all([
        api.get(`/statistics/${selectedServerId.value}/overview`),
        api.get(`/statistics/${selectedServerId.value}/commits?period=${selectedPeriod.value}`),
        api.get(`/statistics/${selectedServerId.value}/repos?limit=10`),
        api.get(`/statistics/${selectedServerId.value}/authors`),
      ]).then(([oRes, cRes, rRes, aRes]) => {
        overview.value = oRes.data
        commitTrend.value = cRes.data
        repoRanking.value = rRes.data
        authorRanking.value = aRes.data
        nextTick(renderChart)
      }).catch(() => {})
    }

    function refreshData() {
      if (!selectedServerId.value) return
      refreshing.value = true
      api.post(`/statistics/${selectedServerId.value}/refresh`).then(() => {
        ElMessage.info('数据采集已触发，请稍后刷新查看')
      }).finally(() => { refreshing.value = false })
    }

    function changePeriod(p) {
      selectedPeriod.value = p
      if (selectedServerId.value) {
        api.get(`/statistics/${selectedServerId.value}/commits?period=${p}`).then(res => {
          commitTrend.value = res.data
          nextTick(renderChart)
        })
      }
    }

    function renderChart() {
      if (!trendChartRef.value || commitTrend.value.length === 0) return
      const container = trendChartRef.value
      container.innerHTML = ''
      const canvas = document.createElement('canvas')
      canvas.width = container.clientWidth || 600
      canvas.height = 300
      container.appendChild(canvas)
      const ctx = canvas.getContext('2d')
      const data = commitTrend.value
      const maxVal = Math.max(...data.map(d => d.commit_count), 1)
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

      const barW = Math.min(40, chartW / data.length - 8)
      data.forEach((d, i) => {
        const x = padding.left + (i + 0.5) * (chartW / data.length) - barW / 2
        const barH = (d.commit_count / maxVal) * chartH
        const y = padding.top + chartH - barH

        const gradient = ctx.createLinearGradient(x, y, x, y + barH)
        gradient.addColorStop(0, '#667eea')
        gradient.addColorStop(1, '#764ba2')
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

    function formatNum(n) {
      if (!n) return '0'
      if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M'
      if (n >= 1000) return (n / 1000).toFixed(1) + 'K'
      return String(n)
    }

    function barWidth(val, list) {
      const max = Math.max(...list.map(i => i.commit_count || i.commits || 0), 1)
      return Math.max(2, (val / max) * 80) + '%'
    }

    function langBarWidth(lines) {
      const max = Math.max(...overview.value.top_languages.map(l => l.lines), 1)
      return Math.max(2, (lines / max) * 100) + '%'
    }

    onMounted(() => {
      api.get('/servers').then(res => {
        servers.value = res.data
        const primary = res.data.find(s => s.role === 'primary')
        if (primary) {
          selectedServerId.value = primary.id
          loadData()
        }
      })
    })

    watch(selectedServerId, () => { loadData() })

    return { servers, selectedServerId, refreshing, overview, commitTrend, repoRanking, authorRanking,
             selectedPeriod, periods, primaryServers, trendChartRef,
             codePct, docPct, otherPct,
             loadData, refreshData, changePeriod, formatNum, barWidth, langBarWidth }
  },
}
</script>

<style scoped>
.section-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px;
}
.section-title { font-size: 18px; font-weight: 700; color: #1a1a2e; margin: 0; }
.header-actions { display: flex; align-items: center; gap: 8px; }
.section-sub-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;
}
.section-sub-title { font-size: 15px; font-weight: 700; color: #1a1a2e; }
.period-tabs { display: flex; gap: 4px; }
.period-btn {
  padding: 4px 14px; border-radius: 6px; border: 1px solid rgba(0,0,0,0.06);
  background: rgba(255,255,255,0.5); cursor: pointer; font-size: 12px; font-weight: 600;
  color: #6b7280; transition: all 0.2s; font-family: inherit;
}
.period-btn:hover { background: rgba(255,255,255,0.85); }
.period-btn.active { background: linear-gradient(135deg, #667eea, #764ba2); color: #fff; border-color: transparent; }

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
.stat-blue { background: linear-gradient(135deg, #89f7fe 0%, #66a6ff 100%); }
.stat-green { background: linear-gradient(135deg, #43e97b 0%, #38f9d7 100%); }
.stat-orange { background: linear-gradient(135deg, #fa709a 0%, #fee140 100%); }
.stat-purple { background: linear-gradient(135deg, #a18cd1 0%, #fbc2eb 100%); }
.stat-icon { font-size: 28px; margin-bottom: 8px; opacity: 0.9; }
.stat-num { font-size: 28px; font-weight: 700; margin-bottom: 2px; letter-spacing: -1px; }
.stat-label { font-size: 13px; opacity: 0.85; font-weight: 500; }

.chart-container { width: 100%; min-height: 300px; }

.rank-list { display: flex; flex-direction: column; gap: 8px; }
.rank-item {
  display: flex; align-items: center; gap: 10px; padding: 8px 12px;
  border-radius: 8px; background: rgba(0,0,0,0.02); position: relative; overflow: hidden;
}
.rank-index { width: 24px; height: 24px; border-radius: 6px; background: linear-gradient(135deg, #667eea, #764ba2); color: #fff; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 700; flex-shrink: 0; }
.rank-name { font-size: 13px; font-weight: 600; color: #1a1a2e; z-index: 1; min-width: 120px; }
.rank-value { font-size: 12px; color: #6b7280; margin-left: auto; z-index: 1; }
.rank-bar { position: absolute; left: 0; top: 0; bottom: 0; background: linear-gradient(90deg, rgba(102,126,234,0.08), rgba(118,75,162,0.04)); border-radius: 8px; z-index: 0; }
.rank-empty { text-align: center; color: #9ca3af; padding: 20px; font-size: 13px; }

.ratio-bar {
  display: flex; height: 36px; border-radius: 8px; overflow: hidden;
  background: rgba(0,0,0,0.04);
}
.ratio-segment {
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 600; color: #fff; transition: width 0.5s ease;
}
.ratio-code { background: linear-gradient(135deg, #667eea, #764ba2); }
.ratio-doc { background: linear-gradient(135deg, #43e97b, #38f9d7); }
.ratio-other { background: linear-gradient(135deg, #a18cd1, #fbc2eb); }

.lang-list { display: flex; flex-direction: column; gap: 8px; }
.lang-item { display: flex; align-items: center; gap: 10px; }
.lang-name { font-size: 13px; font-weight: 600; color: #1a1a2e; min-width: 100px; }
.lang-bar-wrap { flex: 1; height: 8px; background: rgba(0,0,0,0.04); border-radius: 4px; overflow: hidden; }
.lang-bar { height: 100%; background: linear-gradient(90deg, #667eea, #764ba2); border-radius: 4px; transition: width 0.5s ease; }
.lang-lines { font-size: 12px; color: #6b7280; min-width: 70px; text-align: right; }
</style>
