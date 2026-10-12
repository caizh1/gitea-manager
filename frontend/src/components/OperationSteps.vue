<template>
  <el-dialog append-to-body :model-value="true" @update:model-value="$emit('close')" title="执行步骤" style="background: #f7f9fc; --el-dialog-bg-color: #f7f9fc" width="min(880px, 95vw)">
    <el-alert v-if="error" :title="error" type="warning" :closable="false" />
    <p>任务 #{{ task.id }} · {{ task.filename || task.backup_filename }}</p>
    <el-empty v-if="!steps.length" description="暂无历史步骤记录" />
    <section v-for="step in steps" :key="step.id" class="operation-step">
      <div class="step-heading"><el-tag :type="step.status === 'failed' ? 'danger' : step.status === 'running' ? 'warning' : 'success'">{{ operationStatus(step) }}</el-tag><strong>{{ step.label }}</strong></div>
      <p v-if="step.detail">{{ step.detail }}</p>
      <p class="step-time">开始：{{ fmt(step.started_at) }} · 完成：{{ fmt(step.completed_at) }}</p>
      <div v-if="step.metrics" class="step-metrics"><span v-for="(value, key) in step.metrics" :key="key" v-show="value !== null && value !== ''">{{ key }}：{{ value === 'bytes' ? '字节' : value === 'repos' ? '仓库' : value }}</span></div>
      <details v-if="step.stdout_tail || step.stderr_tail"><summary>查看日志摘要</summary><pre>{{ step.stdout_tail }}{{ step.stderr_tail }}</pre></details>
    </section>
  </el-dialog>
</template>
<script setup>
import { ref } from 'vue'
import { api } from '../api'
import { useOperationPolling } from '../composables/useOperationPolling'
import { operationActive, operationStatus, utcTime } from './operationDisplay'
const props = defineProps({ task: { type: Object, required: true }, kind: { type: String, required: true } })
defineEmits(['close'])
const steps = ref([])
const { error } = useOperationPolling(config => api.get(`/${props.kind === 'backup' ? 'backups' : 'restore-tasks'}/${props.task.id}/steps`, config), response => { steps.value = response.data }, () => operationActive(props.task) || steps.value.some(s => s.status === 'running'))
const fmt = value => value ? new Date(utcTime(value)).toLocaleString() : '—'
</script>
<style scoped>
.operation-step{padding:14px 0;border-bottom:1px solid var(--glass-border, #ddd);overflow-wrap:anywhere}.step-heading,.step-metrics{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.step-time,.step-metrics{font-size:12px;color:var(--text-secondary)}pre{white-space:pre-wrap;max-height:220px;overflow:auto}p{margin:8px 0}
</style>
