<template>
  <section class="statistics" aria-label="审查统计概览" :aria-busy="loading">
    <div class="statistics-heading"><div><strong>审查概览</strong><span class="muted">{{ data ? `${data.period.start} — ${data.period.end}` : '所选日期范围' }}</span></div><el-button text @click="$emit('toggle')">{{ collapsed ? '展开统计图表' : '收起统计图表' }}</el-button></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <template v-if="!collapsed">
      <el-skeleton v-if="!data && loading" :rows="5" animated />
      <template v-else-if="data">
        <div class="stat-cards"><article v-for="card in cards" :key="card.label" :class="card.tone"><span>{{ card.label }}</span><strong>{{ card.value ?? '—' }}<small v-if="card.value != null">{{ card.unit }}</small></strong><p>{{ card.note }}</p></article></div>
        <div class="status-summary"><span>所选期间发起 {{ data.summary.total }} 次</span><span v-for="(n, s) in otherStatuses" :key="s">{{ labels[s] || s }} {{ n }}</span><span>成功率 {{ successRate }}</span><span>执行成功不代表没有代码问题</span></div>
        <div class="charts">
          <ReviewStatisticsChart title="每日发起任务的当前结果" :rows="daily" :series="taskSeries" description="按创建日归属；其他状态不计入成功或失败。" />
          <section class="feedback-chart review-chart" aria-label="人工反馈分布"><h4>人工反馈分布</h4><dl class="feedback-items"><div v-for="item in feedbackRows" :key="item.key" class="feedback-item"><dt>{{ item.label }}</dt><dd>{{ item.value }}</dd></div></dl><p class="muted">仅计已有人工反馈；自动修复、PR 合并不算接受。</p></section>
        </div>
        <details class="coverage-notes"><summary>统计口径与数据覆盖</summary><p>任务按创建时间筛选，使用 {{ data.period.timezone }} 自然日。台账问题 {{ data.coverage.tracked }} 个；等级未知 {{ data.coverage.unknown_severity }} 个。</p><p>历史意见 {{ data.coverage.legacy_total }} 条（P0 {{ data.coverage.legacy.P0 || 0 }}，P1 {{ data.coverage.legacy.P1 || 0 }}），未跨任务去重，不混入台账问题数。</p><p>支持接受统计 {{ data.coverage.feedback_supported }} 个；暂无接受数据 {{ data.feedback.unavailable }} 个。当前审查与旧反馈链路尚未兼容，暂无数据不代表用户未接受。</p><p>结果已清理或缺失 {{ data.coverage.missing_results }} 个任务；只有文字报告、没有结构化意见 {{ data.coverage.reports_without_findings }} 个任务，不从正文猜测问题数。</p><p>更新时间：{{ new Date(data.updated_at * 1000).toLocaleString('zh-CN') }}。反馈以最近同步结果为准；停用或待补充记录单列，冲突不计入已接受。</p></details>
        <p v-if="data.feedback.unavailable || !data.coverage.feedback_supported" class="coverage-warning">接受统计覆盖不完整：{{ data.feedback.unavailable }} 个问题暂无接受数据，已接受数仅代表已有人工反馈。</p>
      </template>
    </template>
  </section>
</template>
<script setup>
import { computed } from 'vue'
import ReviewStatisticsChart from './ReviewStatisticsChart.vue'
const props = defineProps({ data: Object, loading: Boolean, error: String, collapsed: Boolean })
defineEmits(['toggle'])
const cards = computed(() => [
  { label: '审查成功', value: props.data.summary.succeeded, unit: '次', tone: 'success', note: '任务已完成' },
  { label: '执行失败', value: props.data.summary.failed, unit: '次', tone: 'failure', note: '取消与待核对单列' },
  { label: 'P0 问题', value: props.data.summary.p0, unit: '个', tone: 'critical', note: '台账去重 · 严重问题' },
  { label: 'P1 问题', value: props.data.summary.p1, unit: '个', tone: 'warning', note: '台账去重 · 高优先级' },
  { label: '用户已接受', value: props.data.summary.accepted, unit: '个', tone: 'accepted', note: props.data.summary.accepted == null ? '暂无可关联的接受数据' : '已有人工“有效”反馈' },
])
const labels = { awaiting_cost:'等待成本确认', budget_paused:'预算暂停', incomplete:'审查未完成', queued:'排队中', running:'审查中', publishing:'发布中', recovering:'核对中', cancelling:'取消中', cancelled:'已取消', superseded:'已过期', publish_unknown:'发布待核对' }
const otherStatuses = computed(() => Object.fromEntries(Object.entries(props.data.statuses).filter(([k]) => !['succeeded','failed'].includes(k))))
const successRate = computed(() => { const n = props.data.summary.succeeded + props.data.summary.failed; return n ? (props.data.summary.succeeded / n * 100).toFixed(1) + '%' : '—' })
const daily = computed(() => props.data.daily.map(row => ({ ...row, label: row.date })))
const taskSeries = [{ key:'succeeded',label:'成功',color:'#35836d' },{ key:'failed',label:'失败',color:'#d66b70' },{ key:'other',label:'其他',color:'#abb8ca' }]
const feedbackRows = computed(() => Object.entries({ accepted:'已接受', false_positive:'误报', other:'其他反馈', conflict:'反馈冲突', withdrawn:'已撤销或失效', pending:'待补充或停用', unreviewed:'尚未反馈', unavailable:'暂无接受数据' }).map(([key,label]) => ({ key,label,value:props.data.feedback[key] || 0 })))
</script>
<style scoped>
.statistics { margin-bottom: 12px; min-width: 0; }.statistics-heading,.statistics-heading>div { display: flex; align-items: center; flex-wrap: wrap; gap: 14px; }.statistics-heading { justify-content: space-between; margin-bottom: 10px; }
.muted { color: #617088; font-size: 12px; line-height: 1.6; }.stat-cards { display: grid; grid-template-columns: repeat(5,minmax(0,1fr)); gap: 12px; }
.stat-cards article { padding: 12px 16px; border: 1px solid rgba(216,227,238,.85); border-radius: 16px; background: rgba(255,255,255,.8); min-width: 0; }.stat-cards span { font-size: 13px; color: #52637b; }.stat-cards strong { display: block; font-size: 26px; margin: 6px 0 4px; font-variant-numeric: tabular-nums; }.stat-cards small { font-size: 12px; font-weight: 400; margin-left: 8px; color: #617088; }.stat-cards p { margin: 0; color: #617088; font-size: 11px; line-height: 1.5; }.success strong { color: #287159; }.failure strong,.critical strong { color: #b54459; }.warning strong { color: #946a25; }.accepted strong { color: #365ba5; }
.status-summary { display: flex; flex-wrap: wrap; gap: 8px 18px; padding: 8px 2px; color: #617088; font-size: 12px; }.charts { display: grid; grid-template-columns: minmax(0,1.6fr) minmax(0,1fr); gap: 12px; }.review-chart { min-width: 0; padding: 12px 14px; border: 1px solid #e3eaf2; border-radius: 14px; background: rgba(255,255,255,.65); }h4 { margin: 0; font-size: 14px; }.feedback-items { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 6px 12px; margin: 10px 0; }.feedback-item { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-width: 0; padding: 5px 8px; border-radius: 6px; background: #f4f7fb; font-size: 12px; color: #52637b; }.feedback-item dt { overflow-wrap: anywhere; }.feedback-item dd { margin: 0; font-weight: 600; font-variant-numeric: tabular-nums; }.feedback-chart>p { margin: 0; }.coverage-notes { margin-top: 12px; color: #617088; font-size: 12px; line-height: 1.7; }summary { cursor: pointer; }.coverage-warning { color: #7c6236; font-size: 12px; margin-bottom: 0; }
@container review-page (max-width: 960px) { .charts { grid-template-columns: 1fr; }.feedback-items { grid-template-columns: repeat(4,minmax(0,1fr)); } }
@container review-page (max-width: 600px) { .stat-cards { grid-template-columns: repeat(2,minmax(0,1fr)); }.feedback-items { grid-template-columns: repeat(2,minmax(0,1fr)); } }
</style>
