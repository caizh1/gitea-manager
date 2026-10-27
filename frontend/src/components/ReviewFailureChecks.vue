<template>
  <section v-if="supported" class="failure-checks" aria-label="自动错误检查" data-testid="failure-checks">
    <div class="checks-heading"><strong>自动错误检查</strong><el-button size="small" :loading="loading" @click="load">重新检查</el-button></div>
    <p class="checks-note">只读检查本任务保存的记录，不调用模型、不改变审查结论、不自动重试。</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="report">
      <p>{{ report.summary }}</p>
      <p v-if="report.status === 'partial' || report.status === 'unavailable'" class="checks-note">部分记录不足或达到检查上限，未检查的范围不作推断。</p>
      <ul><li v-for="row in report.checks" :key="row.code"><div><el-tag size="small" :type="row.state === 'unknown' ? 'warning' : 'info'">{{ labels[row.state] }}</el-tag><strong>{{ row.title }}</strong></div><p>{{ row.detail }}</p></li></ul>
    </template>
    <p v-else-if="loading">正在核对失败会话与保存证据…</p>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api'
import { safeAutomaticChecks } from './reviewDiagnostics'

const props = defineProps({ task: Object })
const types = new Set(['BATCH_OUTPUT_INVALID', 'BATCH_OUTPUT_INCOMPLETE', 'MODEL_INPUT_BUDGET'])
const labels = { confirmed: '已确认的事实', clear: '该项通过', unknown: '仍需确认' }
const supported = computed(() => types.has(props.task?.diagnostic?.error_code) && ['failed', 'incomplete', 'publish_unknown', 'budget_paused'].includes(props.task?.status))
const local = ref(null), error = ref(''), loading = ref(false)
const report = computed(() => local.value || safeAutomaticChecks(props.task?.diagnostic?.automatic_checks))
const identity = computed(() => [props.task?.id, props.task?.diagnostic?.session_id || props.task?.session?.id, props.task?.diagnostic?.error_code, props.task?.status].join(':'))
let controller
async function load() {
  if (!supported.value || loading.value || !props.task?.id) return
  const current = identity.value
  controller = new AbortController(); loading.value = true; error.value = ''
  try {
    const response = await api.get(`/ai-review/tasks/${props.task.id}/failure-checks`, { signal: controller.signal, notifyError: false })
    if (current !== identity.value) return
    local.value = safeAutomaticChecks(response.data)
    if (!local.value) error.value = '自动检查返回了无法识别的结果。'
  } catch (failure) {
    if (current === identity.value && failure.code !== 'ERR_CANCELED') error.value = '自动检查暂不可用，原始错误仍保留。'
  } finally {
    if (current === identity.value) loading.value = false
  }
}
watch(identity, () => {
  controller?.abort(); local.value = null; error.value = ''; loading.value = false
  if (supported.value && !report.value) load()
}, { immediate: true })
onBeforeUnmount(() => controller?.abort())
</script>

<style scoped>
.failure-checks{border:1px solid var(--el-border-color-light);border-radius:12px;padding:16px;margin:12px 0;min-width:0}
.checks-heading,.failure-checks li>div{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.checks-heading{justify-content:space-between}.checks-note{color:var(--el-text-color-secondary);font-size:13px}
.failure-checks ul{padding-left:18px}.failure-checks li{margin:12px 0}.failure-checks p{overflow-wrap:anywhere;margin:8px 0}
</style>
