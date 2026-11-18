<template>
  <div v-if="recovery.state && (task.recovery_pending || task.status === 'failed' || task.failure_at)"
       class="recovery-status" aria-live="polite">
    <div class="recovery-header">
      <el-tag :type="tagType" size="small">{{ label }}</el-tag>
      <span v-if="pending && recovery.action">{{ recovery.action }}</span>
    </div>
    <div v-if="source">{{ source }}<span v-if="recovery.source_checked_at"> · {{ formatTime(recovery.source_checked_at) }}</span></div>
    <div v-if="pending && recovery.error" class="recovery-error">{{ recovery.error }}</div>
    <div v-if="pending && recovery.next_retry_at">下次复查 {{ formatTime(recovery.next_retry_at) }}</div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { utcTime } from './operationDisplay'

const props = defineProps({ task: { type: Object, required: true } })
const recovery = computed(() => props.task.recovery || {})
const pending = computed(() => recovery.value.state !== 'completed')
const label = computed(() => ({
  running: '正在收尾', waiting: '等待重试', blocked: '收尾受阻', completed: '收尾完成',
})[recovery.value.state])
const tagType = computed(() => ({ completed: 'success', blocked: 'danger' })[recovery.value.state] || 'warning')
const source = computed(() => ({
  running: '源 Gitea 容器已运行，API 健康待确认',
  available: '源站已恢复',
  healthy: '源 Gitea API 健康',
  stopped: '最近核实源 Gitea 容器已停止',
})[recovery.value.source_state])
const formatTime = value => new Date(utcTime(value)).toLocaleString()
</script>

<style scoped>
.recovery-status { display: grid; gap: 4px; margin-top: 6px; font-size: 13px; overflow-wrap: anywhere; }
.recovery-header { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.recovery-error { color: var(--el-color-danger); white-space: pre-wrap; }
</style>
