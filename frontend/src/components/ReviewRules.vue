<template>
  <section class="review-rules" data-testid="review-rules">
    <h4>本次自定义规则</h4>
    <p>{{ statusText }}</p>
    <p class="rules-path">{{ rules.path || '.gitea/review-rules.md' }}</p>
    <template v-if="rules.source_commit">
      <p class="rules-source">目标提交 {{ rules.source_commit }}</p>
      <p v-if="rules.sha256" class="rules-source">SHA-256 {{ rules.sha256 }}</p>
    </template>
    <p v-if="rules.error" role="alert">{{ rules.error }}</p>
    <el-button v-if="rules.sha256" :loading="loading" @click="toggleContent">{{ expanded ? '收起规则正文' : '查看冻结规则' }}</el-button>
    <div v-if="expanded" class="rules-content">
      <p v-if="error" role="alert">{{ error }}</p>
      <a v-if="sourceUrl" :href="sourceUrl" target="_blank" rel="noopener noreferrer">查看固定提交文件</a>
      <pre v-if="loaded">{{ content }}</pre>
    </div>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { api } from '../api'

const props = defineProps({ task: { type: Object, required: true } })
const rules = computed(() => props.task.review_rules || { status: 'legacy' })
const statusText = computed(() => {
  if (rules.value.status === 'loaded') return rules.value.submitted ? '已加载并提交模型' : '已加载，等待提交模型'
  return { missing: '未配置，使用默认审查', empty: '内容为空，使用默认审查', failed: '加载失败', pending: '等待读取目标提交', legacy: '历史任务不适用' }[rules.value.status] || '状态未知'
})
const expanded = ref(false), loaded = ref(false), loading = ref(false)
const content = ref(''), sourceUrl = ref(''), error = ref('')
let generation = 0
watch(() => props.task.id, () => {
  generation++
  expanded.value = loaded.value = loading.value = false
  content.value = sourceUrl.value = error.value = ''
})
async function toggleContent() {
  expanded.value = !expanded.value
  if (!expanded.value || loaded.value) return
  const current = ++generation
  loading.value = true
  error.value = ''
  try {
    const { data } = await api.get(`/ai-review/tasks/${props.task.id}/rules`)
    if (current !== generation) return
    content.value = data.content || ''
    if (data.source_url && /^https?:\/\//.test(data.source_url)) sourceUrl.value = data.source_url
    loaded.value = true
  } catch (exception) {
    if (current === generation) error.value = exception.response?.data?.error || '读取冻结规则失败，请重试'
  } finally {
    if (current === generation) loading.value = false
  }
}
</script>

<style scoped>
.review-rules { margin: 16px 0; min-width: 0; }
.rules-path, .rules-source { overflow-wrap: anywhere; font-size: 13px; }
.rules-content { display: flex; flex-direction: column; align-items: flex-start; gap: 12px; margin-top: 12px; min-width: 0; }
pre { box-sizing: border-box; max-width: 100%; width: 100%; max-height: 420px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; padding: 12px; background: #f5f7fa; border-radius: 8px; }
</style>
