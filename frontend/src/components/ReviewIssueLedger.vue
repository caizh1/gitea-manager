<template>
  <section v-if="error || data.total || task.result?.lifecycle || task.progress_stage === 'issue_checks'" class="issue-ledger" data-testid="review-issue-ledger">
    <h4>PR 问题台账</h4>
    <p>旧问题复核 {{ completed }} / {{ checkTotal }}；每个旧问题一次请求，仅对照历史和当前 PR diff。</p>
    <p>状态以依据版本为准。原评论与人工讨论保留；待确认不代表已修复。</p>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-table :data="data.items" size="small" empty-text="尚无可跟踪的独立问题">
      <el-table-column label="问题" min-width="210"><template #default="{ row }"><strong>{{ row.title }}</strong><p>{{ row.path || '未定位' }}</p><small>{{ row.id }}</small></template></el-table-column>
      <el-table-column label="状态与依据" min-width="260"><template #default="{ row }"><el-tag :type="row.stale || row.status === 'uncertain' ? 'warning' : row.status === 'present' ? 'danger' : 'info'">{{ row.label }}</el-tag><p v-if="row.stale">上一版本结论，当前版本待复核</p><p>{{ row.reason }}</p><small>提交 {{ row.head || '历史证据缺失' }}</small><details v-if="row.evidence.length"><summary>查看 diff 证据</summary><p v-for="(e, i) in row.evidence" :key="i"><code>{{ e.id }} {{ e.quote }}</code></p></details></template></el-table-column>
      <el-table-column label="评论同步" min-width="210"><template #default="{ row }"><p v-if="!row.comments.length">尚无行内评论；意见保留在台账与报告</p><p v-for="c in row.comments" :key="c.comment_id">评论 #{{ c.comment_id }}：{{ c.error || '已核对' }}</p></template></el-table-column>
    </el-table>
    <el-pagination v-if="data.total > 50" v-model:current-page="page" layout="prev, pager, next" :page-size="50" :total="data.total" />
    <details v-if="task.result?.lifecycle?.deferred?.length"><summary>候选关系待确认（{{ task.result.lifecycle.deferred.length }} 项，未重复发布）</summary><p v-for="f in task.result.lifecycle.deferred" :key="f.candidate_id">{{ f.title }}：{{ f.reason }}</p></details>
    <p>{{ task.result?.lifecycle?.untracked_notice }}</p>
  </section>
</template>

<script setup>
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import { api } from '../api'
const props = defineProps({ task: { type: Object, required: true }, open: Boolean })
const page = ref(1), data = ref({ items: [], total: 0, checks: {} }), error = ref('')
const completed = computed(() => ['done', 'failed', 'unknown'].reduce((n, s) => n + (data.value.checks[s] || 0), 0))
const checkTotal = computed(() => Object.values(data.value.checks).reduce((n, count) => n + count, 0))
let controller, sequence = 0
async function load() {
  const current = ++sequence
  controller?.abort()
  if (!props.open || !props.task.id) return
  controller = new AbortController()
  try {
    const response = await api.get(`/ai-review/tasks/${props.task.id}/issues`, { params: { page: page.value }, signal: controller.signal, notifyError: false })
    if (current === sequence) { data.value = response.data; error.value = '' }
  } catch (e) { if (current === sequence && e.code !== 'ERR_CANCELED') error.value = e.response?.data?.error || '问题台账暂时无法读取' }
}
watch(() => props.task.id, () => { page.value = 1; data.value = { items: [], total: 0, checks: {} } })
watch([() => props.task.id, () => props.task.updated, () => props.open, page], load, { immediate: true })
onBeforeUnmount(() => { sequence++; controller?.abort() })
</script>

<style scoped>
.issue-ledger { min-width: 0; overflow-wrap: anywhere; margin-bottom: 20px; }
.issue-ledger p { margin: 8px 0; }
.issue-ledger code { white-space: pre-wrap; }
.issue-ledger .el-pagination { display: flex; flex-wrap: wrap; margin-top: 12px; }
</style>
