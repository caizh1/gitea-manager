<template>
  <div class="backup-execution">
    <div class="task-meta">
      <span>备份 #{{ task.id }} · {{ task.source_server_name || task.filename }}</span>
      <el-tag v-if="sourceLabel && !task.recovery?.source_state" :type="sourceType" effect="light">{{ sourceLabel }}</el-tag>
    </div>
    <el-empty v-if="!steps.length && !running" description="暂无历史步骤记录" />
    <ol ref="stepList" class="steps">
      <li v-for="step in steps" :key="step.id">
        <section v-if="step === current" class="live-step" aria-label="当前执行步骤">
          <div class="step-heading">
            <LoaderCircle class="step-icon spinning" :size="24" aria-hidden="true" />
            <h4>{{ step.label }}</h4>
            <span class="timing">阶段耗时 {{ elapsedText(step.started_at, now) }}</span>
          </div>

          <div v-if="counts.current != null" class="quantity">
            <div class="quantity-line">
              <span>{{ counts.unit === 'repos' ? '已完成' : '已处理' }} <strong>{{ quantity }}</strong></span>
              <strong v-if="percent != null">{{ percent }}%</strong>
            </div>
            <el-progress v-if="percent != null" :percentage="percent" :show-text="false" :stroke-width="9"
                         :aria-label="step.label + '进度'" />
          </div>

          <p v-if="activity.artifacts">{{ artifactProgressText(activity.artifacts) }}</p>
          <div class="live-details">
            <div>
              <template v-if="counts.item">
                <div class="muted">{{ counts.unit === 'repos' ? '当前仓库' : '当前文件' }}</div>
                <code class="item">{{ counts.item }}</code>
              </template>
              <div v-else class="muted">当前动作</div>
              <div class="action">
                <LoaderCircle v-if="counts.item" class="step-icon spinning" :size="17" aria-hidden="true" />
                <strong>{{ step.detail || '等待步骤上报' }}</strong>
                <span v-if="counts.item && activity.action_started_at" class="timing">
                  本项耗时 {{ elapsedText(activity.action_started_at, now) }}
                </span>
              </div>
            </div>
            <div v-if="step.step_key === 'dump' && activity.zip_bytes != null" class="zip-data">
              <div class="muted">已观测 ZIP 大小</div>
              <div class="zip-size">
                <strong>{{ archiveSize(activity.zip_bytes) }}</strong>
                <span v-if="activity.zip_delta > 0" class="muted">
                  近 {{ activity.zip_window_seconds }} 秒 +{{ archiveSize(activity.zip_delta) }}
                </span>
              </div>
            </div>
          </div>

          <div v-if="step.step_key === 'transfer' && activity.bytes_per_second > 0" class="timing">
            {{ archiveSize(activity.bytes_per_second) }}/秒
            <span v-if="activity.eta_seconds != null"> · 预计剩余 {{ Math.ceil(activity.eta_seconds / 60) }} 分钟</span>
          </div>
          <div class="observation">
            <span class="observation-state" :class="{ uncertain: stale || error }">
              <Circle :size="8" class="observation-dot" fill="currentColor" aria-hidden="true" />
              {{ observation }}
            </span>
            <span v-if="activity.observed_at" class="timing">最近观测 {{ clockTime(activity.observed_at) }}</span>
          </div>
        </section>

        <section v-else class="step-row" :class="{ failed: step.status === 'failed' }">
          <div class="step-heading">
            <CircleCheck v-if="step.status === 'success'" class="step-icon done" :size="19" aria-hidden="true" />
            <CircleAlert v-else-if="step.status === 'failed'" class="step-icon failed-icon" :size="19" aria-hidden="true" />
            <Circle v-else class="step-icon pending" :size="19" aria-hidden="true" />
            <h4>{{ step.label }}</h4>
            <span v-if="step.status === 'success' && stepCounts(step).unit === 'repos'" class="done">
              {{ quantityText(progressShape(stepCounts(step))) }}
            </span>
            <span v-else-if="step.status === 'failed'" class="failed-icon">失败</span>
            <span v-else-if="step.completed_at" class="timing">{{ elapsedText(step.started_at, utcTime(step.completed_at)) }}</span>
          </div>
          <p v-if="step.metrics?.实时?.artifacts">{{ artifactProgressText(step.metrics.实时.artifacts) }}</p>
          <p v-if="step.status === 'failed' && step.detail" class="failure-detail">{{ step.detail }}</p>
        </section>
      </li>

      <li v-if="running && !current" class="step-row pending">
        {{ task.progress_label || '等待步骤上报' }}
      </li>
      <li v-for="(label, index) in upcoming.slice(0, 3)" :key="label" class="step-row">
        <div class="step-heading">
          <Circle class="step-icon pending" :size="19" aria-hidden="true" />
          <h4>{{ label }}</h4>
          <span v-if="index === 0" class="timing">待执行</span>
        </div>
      </li>
      <li v-if="upcoming.length > 3">
        <details class="planned-steps">
          <summary><ChevronDown :size="15" aria-hidden="true" />其余 {{ upcoming.length - 3 }} 个待执行步骤</summary>
          <ol>
            <li v-for="label in upcoming.slice(3)" :key="label" class="step-row">
              <div class="step-heading">
                <Circle class="step-icon pending" :size="19" aria-hidden="true" />
                <h4>{{ label }}</h4>
              </div>
            </li>
          </ol>
        </details>
      </li>
    </ol>
    <el-alert v-if="task.actions_warning" type="warning" :closable="false"
              :title="`源端已缺失 ${task.actions_warning.count} 件 Actions 制品`">
      <details>
        <summary>查看缺失明细</summary>
        <ul><li v-for="path in task.actions_warning.examples" :key="path">{{ path }}</li></ul>
        <p v-if="task.actions_warning.count > task.actions_warning.examples.length">完整清单随备份保存</p>
      </details>
    </el-alert>
    <el-alert v-if="task.error_msg" :title="task.error_msg" type="error" :closable="false" class="task-error">
      <template v-if="task.failure_label || task.failure_at">
        {{ task.failure_label }}<span v-if="task.failure_at"> · {{ clockTime(task.failure_at) }}</span>
      </template>
    </el-alert>
    <BackupRecoveryStatus :task="task" />
  </div>
</template>

<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { ChevronDown, Circle, CircleAlert, CircleCheck, LoaderCircle } from '@lucide/vue'
import BackupRecoveryStatus from './BackupRecoveryStatus.vue'
import { artifactProgressText, backupObservationText, backupObservationStale, elapsedText, operationActive, quantityText, stagePercent, utcTime } from './operationDisplay'

const props = defineProps({
  task: { type: Object, required: true },
  steps: { type: Array, required: true },
  error: { type: String, default: '' },
})

const stages = [
  ['prepare', '准备备份任务'],
  ['stop_source', '停止源 Gitea'],
  ['snapshot', '记录停写状态下的仓库与引用'],
  ['snapshot_tables', '记录数据库校验基准'],
  ['dump', '导出同一停写点的数据库和文件'],
  ['snapshot_verify', '复核仓库与引用'],
  ['snapshot_actions', '生成源端 Actions 制品清单'],
  ['freeze_export', '固化源端备份副本'],
  ['resume_source', '恢复源站服务'],
  ['transfer', '源站已恢复，传输固定副本'],
  ['save_archive', '保存本地 ZIP'],
  ['assemble', '整理备份内容'],
  ['manifest', '校验归档内容并生成清单'],
  ['commit_snapshot', '保存 Commit 校验记录'],
  ['checksum', '生成备份校验信息'],
]
const running = computed(() => operationActive(props.task))
const newestSteps = computed(() => [...props.steps].reverse())
const current = computed(() => running.value ? newestSteps.value.find(step => step.status === 'running') : null)
const activity = computed(() => current.value?.metrics?.实时 || {})
const counts = computed(() => stepCounts(current.value))
const percent = computed(() => stagePercent(progressShape(counts.value)))
const quantity = computed(() => counts.value.unit === 'bytes' && counts.value.total == null
  ? archiveSize(counts.value.current)
  : quantityText(progressShape(counts.value)))
const now = ref(Date.now())
const stale = computed(() => backupObservationStale(activity.value, now.value))
const observation = computed(() => backupObservationText(
  activity.value, now.value, Boolean(props.error), current.value?.step_key,
))
const source = computed(() => newestSteps.value.find(step => step.metrics?.实时?.source_state)?.metrics.实时.source_state)
const sourceLabel = computed(() => {
  if (source.value === 'available') return '源站已恢复'
  if (source.value === 'stopped') return '源站备份前已停止'
  if (!running.value && source.value) return '源站状态待确认'
  if (source.value === 'paused') return props.error || stale.value ? '上次确认源站停写' : '源站停写中'
  if (source.value === 'recovering') return props.error || stale.value ? '上次确认源站恢复中' : '源站恢复中'
  return ''
})
const sourceType = computed(() => source.value === 'available' ? 'success' : source.value === 'stopped' ? 'info' : 'warning')
const upcoming = computed(() => {
  const index = stages.findIndex(([key]) => key === (current.value?.step_key || props.task.progress_stage))
  return running.value && index >= 0 ? stages.slice(index + 1).map(([, label]) => label) : []
})

const stepList = ref(null)
watch(() => current.value?.id, () => {
  const list = stepList.value
  const row = list?.querySelector('.live-step')
  if (!row) return
  const area = list.getBoundingClientRect()
  const top = row.getBoundingClientRect().top
  if (top < area.top || top + row.offsetHeight > area.bottom) {
    list.scrollTop += top - area.top
  }
}, { flush: 'post' })

let timer
watch(running, active => {
  clearInterval(timer)
  now.value = Date.now()
  if (active) timer = setInterval(() => { now.value = Date.now() }, 1000)
}, { immediate: true })
onUnmounted(() => clearInterval(timer))

function stepCounts(step) {
  const metrics = step?.metrics || {}
  return { current: metrics.当前数量, total: metrics.总数量, unit: metrics.单位, item: metrics.处理对象 }
}

function progressShape({ current, total, unit }) {
  return { progress_current: current, progress_total: total, progress_unit: unit }
}

function archiveSize(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(0)} MB`
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`
}

function clockTime(value) {
  return new Date(utcTime(value)).toLocaleTimeString('zh-CN', { hour12: false })
}
</script>

<style scoped>
.backup-execution { color: var(--text-primary, #17233b); }
.task-meta { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px 12px; margin: 0 0 14px; color: #43536e; font-size: 15px; }
.task-meta > span:first-child { min-width: 0; overflow-wrap: anywhere; }
.steps { display: grid; gap: 8px; list-style: none; margin: 0; padding: 0 3px; max-height: calc(90vh - 160px); overflow-y: auto; }
.step-row, .live-step { border-radius: 10px; }
.step-row { background: #edf2f9; padding: 9px 14px; }
.step-heading { display: flex; align-items: center; flex-wrap: wrap; gap: 7px 10px; }
.step-heading h4 { flex: 1 1 11rem; min-width: 0; margin: 0; font-size: 16px; font-weight: 600; overflow-wrap: anywhere; }
.step-icon { flex: 0 0 auto; }
.done { color: #187537; }
.pending, .muted, .timing { color: #51627d; }
.failed { background: #fff1f0; }
.failed-icon { color: #a92525; }
.task-error { margin-top: 12px; overflow-wrap: anywhere; }
.failure-detail { margin: 8px 0 0; overflow-wrap: anywhere; white-space: pre-wrap; }
.live-step { background: #edf5ff; border: 1px solid #88baff; padding: 24px; }
.live-step .step-heading h4 { color: #125bbd; font-size: 22px; }
.spinning { color: #096ee0; animation: backup-step-spin 1.2s linear infinite; }
@keyframes backup-step-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spinning { animation: none; } }
.timing, .muted { font-size: 13px; }
.quantity { margin-top: 18px; }
.quantity-line { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; margin-bottom: 6px; }
.quantity-line strong, .zip-size strong { font-size: 32px; font-variant-numeric: tabular-nums; }
.live-details { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 230px), 1fr)); gap: 12px 20px; margin-top: 16px; }
.quantity + .live-details, .observation { border-top: 1px solid #b9d3f3; padding-top: 12px; }
.item { display: block; margin: 3px 0; white-space: normal; overflow-wrap: anywhere; font-size: 15px; }
.action, .zip-size, .observation, .observation-state { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 10px; }
.action { margin-top: 3px; }
.zip-data { border-inline-start: 1px solid #b9d3f3; padding-inline-start: 18px; }
.action strong { overflow-wrap: anywhere; }
.action .timing { margin-left: auto; }
.observation { margin-top: 12px; justify-content: space-between; }
.observation-state { font-size: 13px; }
.observation-dot { flex: 0 0 auto; color: #096ee0; }
.uncertain .observation-dot { color: #a75f05; }
.planned-steps summary { display: flex; align-items: center; gap: 6px; width: fit-content; padding: 4px; color: #235d9d; cursor: pointer; }
.planned-steps summary::-webkit-details-marker { display: none; }
.planned-steps[open] summary svg { transform: rotate(180deg); }
.planned-steps ol { display: grid; gap: 8px; list-style: none; margin: 6px 0 0; padding: 0; }
@media (max-width: 560px) {
  .live-step { padding: 12px; }
  .live-step .step-heading h4 { font-size: 15px; }
  .live-step .step-heading .timing { flex-basis: 100%; }
  .step-heading h4 { font-size: 14px; }
  .zip-data { border-inline-start: 0; padding-inline-start: 0; }
}
</style>
