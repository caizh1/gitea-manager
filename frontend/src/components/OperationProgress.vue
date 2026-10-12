<template>
  <div class="operation-progress">
    <div class="operation-heading" aria-live="polite"><el-tag :type="active ? 'warning' : failed ? 'danger' : 'success'" size="small">{{ operationStatus(task) }}</el-tag><span>{{ task.progress_label || (active ? '等待步骤上报' : '') }}</span></div>
    <template v-if="active || task.progress_stage">
      <el-progress v-if="percent !== null || !active" :percentage="!active && !failed ? 100 : percent ?? 0" :status="failed ? 'exception' : !active ? 'success' : undefined" :show-text="false" :stroke-width="7" />
      <div v-else class="indeterminate" role="progressbar" aria-label="当前步骤执行中，进度暂不可计量"><span /></div>
      <div class="operation-count"><span>{{ quantityText(task) }}<template v-if="active && percent !== null"> · 阶段 {{ percent }}%</template></span><span>耗时 {{ elapsed }}</span></div>
      <div v-if="task.progress_item" class="operation-detail">{{ task.progress_item }}</div>
      <div v-if="task.progress_detail && task.progress_detail !== task.progress_item" class="operation-detail">{{ task.progress_detail }}</div>
      <div v-if="task.progress_updated_at" class="operation-time">最近上报 {{ new Date(utcTime(task.progress_updated_at)).toLocaleTimeString() }}</div>
    </template>
    <el-button v-if="kind" link type="primary" size="small" @click="details = true">展开详情</el-button>
    <OperationSteps v-if="details" :task="task" :kind="kind" @close="details = false" />
  </div>
</template>
<script setup>
import { ref, computed, watch, onUnmounted } from 'vue'
import OperationSteps from './OperationSteps.vue'
import { operationActive, operationStatus, stagePercent, quantityText, elapsedText, utcTime } from './operationDisplay'
const props = defineProps({ task: { type: Object, required: true }, kind: { type: String, default: '' } })
const details = ref(false), now = ref(Date.now())
const active = computed(() => operationActive(props.task))
const failed = computed(() => (props.task.status || props.task.last_status) === 'failed')
const percent = computed(() => stagePercent(props.task))
const elapsed = computed(() => elapsedText(props.task.started_at || props.task.progress_step_started_at, active.value ? now.value : utcTime(props.task.completed_at || props.task.progress_updated_at) || now.value))
let timer
watch(active, value => { clearInterval(timer); now.value = Date.now(); if (value) timer = setInterval(() => { now.value = Date.now() }, 1000) }, { immediate: true })
onUnmounted(() => clearInterval(timer))
</script>
<style scoped>
.operation-progress{min-width:0;padding:5px 0;line-height:1.5}.operation-heading,.operation-count{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:5px}.operation-heading{justify-content:flex-start;font-size:12px}.operation-count,.operation-detail,.operation-time{font-size:12px;overflow-wrap:anywhere}.operation-count,.operation-time{color:var(--text-secondary)}.operation-detail{max-height:4.5em;overflow:auto}.indeterminate{display:flex;height:7px;overflow:hidden;border-radius:4px;background:var(--el-fill-color-light,#e8eef8)}.indeterminate span{width:100%;background:linear-gradient(100deg,transparent 10%,#409eff 45%,transparent 80%);background-size:200% 100%;animation:operation-flow 1.7s linear infinite}@keyframes operation-flow{from{background-position:100% 0}to{background-position:-100% 0}}@media(prefers-reduced-motion:reduce){.indeterminate span{animation:none}}
</style>
