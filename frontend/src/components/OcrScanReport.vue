<template>
  <div class="ocr-report">
    <section class="glass-card ocr-report-overview" aria-label="报告概览">
      <div class="ocr-report-heading">
        <div class="ocr-report-identity">
          <div class="ocr-report-repository"><h2>{{ report.repository }}</h2><span>{{ report.branch }}</span></div>
          <div class="ocr-report-commit"><span>固定提交</span><code>{{ report.sha }}</code></div>
        </div>
        <div class="ocr-export-control" role="group" aria-label="报告导出" :aria-busy="busy">
          <button type="button" class="ocr-export-button" :disabled="busy" @click="emit('download', 'html')">
            <Download :size="17" aria-hidden="true" /><span>导出 HTML</span>
          </button>
          <el-dropdown trigger="click" placement="bottom-end" popper-class="ocr-report-export-menu"
                       :disabled="busy" @command="emit('download', $event)">
            <button type="button" class="ocr-export-button ocr-export-toggle" aria-label="其他导出格式" :disabled="busy">
              <ChevronDown :size="16" aria-hidden="true" />
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item v-for="format in exportFormats" :key="format.value" :command="format.value" :disabled="!format.available"
                                  :aria-label="'导出 ' + format.label">
                  <span class="ocr-export-option">
                    <component :is="format.icon" :size="15" aria-hidden="true" />
                    <span>{{ format.label }}</span><small v-if="!format.available">未生成</small>
                  </span>
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>
      <ul class="ocr-report-metrics" aria-label="报告统计" role="list">
        <li v-for="metric in metrics" :key="metric.label" class="ocr-report-metric">
          <span class="ocr-report-metric-icon" :class="metric.tone" aria-hidden="true">
            <component :is="metric.icon" :size="20" />
          </span>
          <div><span>{{ metric.label }}</span><strong>{{ metric.value }}</strong></div>
        </li>
      </ul>
    </section>

    <section class="ocr-report-issues" aria-labelledby="ocr-report-issues-title">
      <div class="ocr-issues-heading">
        <div class="ocr-issues-title">
          <h2 id="ocr-report-issues-title">问题清单</h2>
          <span aria-live="polite">{{ matchCount }}</span>
        </div>
        <div class="ocr-issues-filters">
          <el-input v-model="query" clearable placeholder="搜索文件或问题" aria-label="搜索报告问题">
            <template #prefix><Search :size="17" aria-hidden="true" /></template>
          </el-input>
          <el-select v-model="selectedLevel" placeholder="全部严重程度" aria-label="筛选严重程度">
            <el-option label="全部严重程度" value="" />
            <el-option v-for="label in severityOptions" :key="label" :label="label" :value="label" />
          </el-select>
        </div>
      </div>
      <div v-if="!visibleFindings.length" class="glass-card">
        <el-empty description="没有匹配的发现" :image-size="70" />
      </div>
      <article v-for="finding in visibleFindings" :key="finding.index" class="ocr-finding">
        <header class="ocr-finding-header">
          <div class="ocr-finding-location">
            <span class="ocr-finding-icon" aria-hidden="true"><FileCode :size="18" /></span>
            <component :is="finding.link ? 'a' : 'span'" class="ocr-source-location" :href="finding.link || undefined"
                       :target="finding.link ? '_blank' : undefined" :rel="finding.link ? 'noopener noreferrer' : undefined">
              <code>{{ finding.path || '未提供文件' }}</code>
              <span v-if="finding.line > 0" class="ocr-finding-line">第 {{ finding.line }} 行</span>
              <ArrowUpRight v-if="finding.link" :size="15" aria-hidden="true" />
            </component>
          </div>
          <span class="ocr-finding-severity" :class="finding.tone">
            <span aria-hidden="true"></span>{{ finding.label }}
          </span>
        </header>
        <OcrReportText :blocks="finding.content_blocks" :raw="finding.content" />
        <div v-if="finding.source || finding.suggestion" class="ocr-finding-snippets" :class="{ 'has-pair': finding.source && finding.suggestion }">
          <OcrReportCode v-if="finding.source" :text="finding.source" label="相关代码" />
          <OcrReportCode v-if="finding.suggestion" :text="finding.suggestion" label="修改建议" suggestion />
        </div>
      </article>
    </section>

    <section class="glass-card ocr-report-details">
      <details v-if="report.summary"><summary>OCR 汇总</summary>
        <OcrReportText :blocks="report.summary_blocks" :raw="report.summary" />
      </details>
      <details><summary>文件覆盖明细</summary>
        <OcrFileScope :files="report.scope" completed />
      </details>
    </section>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { ArrowUpRight, ChevronDown, Clock3, Coins, Download, FileCode, Folder, Layers, Search, TriangleAlert } from '@lucide/vue'
import OcrFileScope from './OcrFileScope.vue'
import OcrReportCode from './OcrReportCode.vue'
import OcrReportText from './OcrReportText.vue'

const props = defineProps({ report: { type: Object, required: true }, exportFormats: { type: Array, required: true }, busy: Boolean })
const emit = defineEmits(['download'])
const query = ref('')
const selectedLevel = ref('')
const numbers = new Intl.NumberFormat('zh-CN')
const severity = new Map([
  ['critical', ['严重', 'is-danger']], ['error', ['高风险', 'is-danger']], ['high', ['高风险', 'is-danger']],
  ['medium', ['中风险', 'is-warning']], ['warning', ['中风险', 'is-warning']], ['low', ['低风险', 'is-low']],
])

const metrics = computed(() => {
  const row = props.report
  return [
    { label: '文件覆盖', value: `${numbers.format(row.completed)} / ${numbers.format(row.total)}`, icon: Folder, tone: 'is-blue' },
    { label: '已排除', value: numbers.format(row.excluded), icon: Layers, tone: 'is-purple' },
    { label: '发现问题', value: numbers.format(row.findings.length), icon: TriangleAlert, tone: 'is-amber' },
    { label: '已用 Token', value: numbers.format(row.tokens), icon: Coins, tone: 'is-green' },
    { label: '执行耗时', value: numbers.format(row.seconds) + ' 秒', icon: Clock3, tone: 'is-blue' },
  ]
})
const findings = computed(() => props.report.findings.map((finding, index) => {
  const [label, tone] = severity.get(finding.severity.toLowerCase()) || [finding.severity_label || finding.severity, 'is-neutral']
  return { ...finding, index, label, tone }
}))
const severityOptions = computed(() => [...new Set(findings.value.map(finding => finding.label))])
const visibleFindings = computed(() => {
  const term = query.value.trim().toLowerCase()
  return findings.value.filter(finding => (!selectedLevel.value || finding.label === selectedLevel.value) &&
    [finding.path, finding.content].some(value => value.toLowerCase().includes(term)))
})
const matchCount = computed(() => visibleFindings.value.length === findings.value.length
  ? `${numbers.format(findings.value.length)} 项`
  : `${numbers.format(visibleFindings.value.length)} / ${numbers.format(findings.value.length)} 项`)
</script>

<style scoped>
.ocr-report { display: grid; gap: 18px; min-width: 0; }
.ocr-report-overview, .ocr-report-issues { container-type: inline-size; }
.ocr-report-overview, .ocr-report-details { padding: 18px; min-width: 0; }
.ocr-report-heading, .ocr-issues-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
}
.ocr-report-identity { min-width: 0; }
.ocr-report-repository, .ocr-report-commit { display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px 10px; }
.ocr-report-repository h2 { margin: 0; color: #142239; font-size: 20px; overflow-wrap: anywhere; }
.ocr-report-repository > span { padding: 2px 8px; border-radius: 9px; color: #194b8b; background: #dfeafd; font-size: 13px; font-weight: 650; overflow-wrap: anywhere; }
.ocr-report-commit { margin-top: 7px; color: #405671; font-size: 12px; }
.ocr-report-commit code { padding: 2px 7px; border-radius: 5px; background: #eaf1fa; overflow-wrap: anywhere; }
.ocr-export-control { display: inline-flex; flex: 0 0 auto; border-radius: 9px; background: #1460b9; }
.ocr-export-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  min-height: 38px;
  padding: 0 12px;
  border: 0;
  border-radius: 9px 0 0 9px;
  color: #fff;
  background: transparent;
  cursor: pointer;
  font: inherit;
  font-size: 13px;
  font-weight: 650;
}
.ocr-export-button:hover:not(:disabled), .ocr-export-button[aria-expanded="true"] { background: #104e9f; }
.ocr-export-button:focus-visible { outline: 2px solid #fff; outline-offset: -4px; }
.ocr-export-button:disabled { opacity: .7; cursor: wait; }
.ocr-export-toggle { padding: 0 9px; border-inline-start: 1px solid #ffffff55; border-radius: 0 9px 9px 0; }
.ocr-export-option { display: flex; align-items: center; gap: 8px; width: 100%; }
.ocr-export-option svg { flex: 0 0 auto; }
.ocr-export-option small { margin-inline-start: auto; padding-inline-start: 12px; }
:global(.ocr-report-export-menu .el-dropdown-menu__item) { min-width: 165px; }
:global(.ocr-report-export-menu .el-dropdown-menu__item.is-disabled) { color: #5e7188; }
.ocr-report-metrics {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 10px;
  margin: 15px 0 0;
  padding: 13px 0 0;
  border-top: 1px solid #d9e4f1;
  list-style: none;
}
.ocr-report-metric {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 9px;
  padding: 10px;
  border: 1px solid #d6e2f2;
  border-radius: 10px;
  background: #edf4fd;
}
.ocr-report-metric-icon { display: grid; place-items: center; flex: 0 0 34px; height: 34px; border-radius: 8px; }
.ocr-report-metric-icon.is-blue { color: #1557b4; background: #dceafd; }
.ocr-report-metric-icon.is-purple { color: #6840b8; background: #ebe3fb; }
.ocr-report-metric-icon.is-amber { color: #994e10; background: #fbe8d8; }
.ocr-report-metric-icon.is-green { color: #14723f; background: #dcf2e5; }
.ocr-report-metric > div { min-width: 0; }
.ocr-report-metric > div > span { display: block; color: #344b66; font-size: 12px; font-weight: 600; }
.ocr-report-metric strong { display: block; margin-top: 3px; color: #162841; font-size: 21px; line-height: 1.15; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.ocr-issues-heading { padding: 2px 0 11px; }
.ocr-issues-title { display: flex; align-items: baseline; gap: 8px; }
.ocr-issues-title h2 { margin: 0; color: #142239; font-size: 18px; }
.ocr-issues-title span { color: #405671; font-size: 13px; font-variant-numeric: tabular-nums; }
.ocr-issues-filters { display: grid; grid-template-columns: minmax(0, 1fr) minmax(115px, 150px); gap: 9px; width: min(100%, 410px); }
.ocr-issues-filters :deep(.el-input__wrapper), .ocr-issues-filters :deep(.el-select__wrapper) {
  min-height: 38px;
  border-radius: 8px !important;
  background: #fff !important;
  box-shadow: inset 0 0 0 1px #cbd8e8 !important;
}
.ocr-issues-filters :deep(.el-input__wrapper.is-focus), .ocr-issues-filters :deep(.el-select__wrapper.is-focused) {
  box-shadow: inset 0 0 0 1px #2864ba, 0 0 0 2px #dbe9fc !important;
}
.ocr-issues-filters :deep(.el-input__prefix-inner > svg) { margin-inline-end: 7px; color: #455b77; }
.ocr-issues-filters :deep(.el-select__placeholder), .ocr-issues-filters :deep(.el-input__inner::placeholder) { color: #405671; }
.ocr-finding { margin-bottom: 11px; padding: 14px; border: 1px solid #d6e2f2; border-radius: 12px; background: #f9fcff; }
.ocr-finding:last-child { margin-bottom: 0; }
.ocr-finding-header, .ocr-finding-location, .ocr-source-location { display: flex; align-items: center; gap: 8px; min-width: 0; }
.ocr-finding-header { justify-content: space-between; gap: 10px; padding-bottom: 10px; border-bottom: 1px solid #e1e9f3; }
.ocr-finding-location { flex: 1 1 0; }
.ocr-finding-icon { display: grid; place-items: center; flex: 0 0 30px; height: 30px; border-radius: 8px; color: #214e86; background: #e1edfc; }
.ocr-source-location { flex-wrap: wrap; color: #223b5a; font-size: 13px; }
.ocr-source-location code { min-width: 0; font-weight: 700; overflow-wrap: anywhere; }
a.ocr-source-location { color: #1553a3; text-decoration: none; }
a.ocr-source-location:hover code, a.ocr-source-location:hover .ocr-finding-line { text-decoration: underline; }
.ocr-source-location svg { flex: 0 0 auto; }
.ocr-finding-line { border-radius: 5px; padding: 2px 5px; background: #edf3fb; white-space: nowrap; }
.ocr-finding-severity {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 5px;
  padding: 3px 8px;
  border-radius: 10px;
  color: #37506c;
  background: #e9eef4;
  font-size: 12px;
  font-weight: 650;
}
.ocr-finding-severity > span { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.ocr-finding-severity.is-low { color: #174f9a; background: #e5efff; }
.ocr-finding-severity.is-warning { color: #87430d; background: #fff0d9; }
.ocr-finding-severity.is-danger { color: #9c263d; background: #fce8ed; }
.ocr-finding .ocr-report-text { color: #283d55; font-size: 14px; }
.ocr-finding-snippets { display: grid; grid-template-columns: minmax(0, 1fr); gap: 10px; margin-top: 11px; }
.ocr-finding-snippets.has-pair { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.ocr-report-details { padding-block: 4px; }
.ocr-report-details details { padding-block: 10px; }
.ocr-report-details details + details { border-top: 1px solid var(--el-border-color-lighter); }
.ocr-report-details summary { color: #223b5a; cursor: pointer; font-weight: 650; }
.ocr-report-details details[open] summary { margin-bottom: 8px; }
@container (max-width: 820px) {
  .ocr-report-metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@container (max-width: 540px) {
  .ocr-report-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; }
  .ocr-report-metric:last-child { grid-column: 1 / -1; }
  .ocr-report-metric { padding: 8px; }
  .ocr-report-metric strong { font-size: clamp(14px, 5.4cqi, 20px); }
  .ocr-issues-filters { width: 100%; grid-template-columns: minmax(0, 1fr) minmax(115px, 132px); }
  .ocr-finding-snippets.has-pair { grid-template-columns: minmax(0, 1fr); }
}
@container (max-width: 265px) {
  .ocr-issues-filters { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 790px) {
  .ocr-report-overview, .ocr-finding { padding: 12px; }
}
</style>
