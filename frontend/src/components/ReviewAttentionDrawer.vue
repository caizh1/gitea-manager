<template>
  <el-drawer v-model="visible" :title="selected ? '处理 Review 待办' : '全部 Review 待办'" size="min(760px, 96vw)">
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <template v-if="selected">
      <div class="drawer-heading"><div><h3>{{ group?.repo_name || `仓库 ${repository}` }} #{{ number }}</h3><p>{{ pendingTotal }} 项发布结果待核对，{{ waitingTotal }} 条后续审查正在等待</p></div><el-button link type="primary" @click="router.push(attentionRoute())">全部待办</el-button></div>
      <el-alert v-if="worker && !worker.available" :title="worker.message" type="warning" :closable="false" />
      <el-alert v-if="loaded && !pendingTotal && !error" title="该 PR 的待核对事项已处理完毕" description="关联排队任务将由工作进程按正常顺序领取。" type="success" :closable="false" />
      <p class="drawer-help">先读取 Gitea 的实际发布结果。只有确认原请求已经结束且未发布，才可以确认未发布并解除阻塞。</p>
      <div v-loading="loading && !loaded" class="pending-tasks">
        <article v-for="task in pendingTasks" :key="task.id" class="pending-task">
          <div class="task-heading"><strong>版本 {{ task.progress?.head?.slice(0, 12) || '尚未读取' }}</strong><el-tag :type="task.reconciliation?.state === 'failed' ? 'danger' : 'warning'">{{ reconcileLabel(task) }}</el-tag></div>
          <p class="task-id">任务 {{ task.id }}</p>
          <p class="task-message">{{ actionErrors[task.id] || task.reconciliation?.message || task.error || '需要核对前次发布结果' }}</p>
          <div class="task-times"><span>请求核对：{{ fmt(task.reconciliation?.requested_at) }}</span><span>最近完成：{{ fmt(task.reconciliation?.finished_at) }}</span></div>
          <div class="task-actions"><el-button type="primary" :loading="!!actionBusy[task.id]" :disabled="['queued', 'checking'].includes(task.reconciliation?.state)" @click="check(task)">核对发布</el-button><el-button :loading="!!actionBusy[task.id]" @click="confirm(task)">确认未发布</el-button><el-button link @click="detailTask = task">任务详情</el-button></div>
        </article>
      </div>
      <el-pagination v-if="pendingTotal > 20" v-model:current-page="pendingPage" :page-size="20" :total="pendingTotal" layout="prev, pager, next, total" @current-change="load(true)" />
      <section v-if="waitingTotal" class="waiting-tasks"><h4>关联等待任务 · {{ waitingTotal }}</h4><div v-for="task in waitingTasks" :key="task.id" class="waiting-row"><code>{{ task.progress?.head?.slice(0, 12) || '尚未读取' }}</code><span>{{ task.progress?.detail || task.stage }}</span></div><el-pagination v-if="waitingTotal > 20" v-model:current-page="waitingPage" :page-size="20" :total="waitingTotal" layout="prev, next, total" @current-change="load()" /></section>
    </template>
    <template v-else>
      <p class="drawer-help">按 PR 汇总全部未解决事项，优先显示正在阻塞后续审查的 PR。</p>
      <div v-loading="loading && !loaded"><article v-for="item in items" :key="`${item.repository}-${item.number}`" class="pending-task"><div class="task-heading"><strong>{{ item.repo_name }} #{{ item.number }}</strong><el-tag type="danger">{{ item.blocked_count ? `阻塞 ${item.blocked_count} 条审查` : '需要核对' }}</el-tag></div><p class="task-message">{{ item.pending_count }} 项发布结果待核对 · 已等待 {{ attentionWait(item.since) }}</p><el-button type="primary" @click="router.push(attentionRoute(item))">立即处理</el-button></article></div>
      <el-empty v-if="loaded && !items.length && !error" description="当前没有需要处理的 Review 待办" />
      <el-pagination v-if="total > 20" v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next, total" @current-change="load()" />
    </template>
    <el-dialog v-model="detailVisible" title="待核对任务详情" width="min(620px, 94vw)" append-to-body><template v-if="detailTask"><p class="task-id">任务 {{ detailTask.id }}</p><p class="task-id">Head：{{ detailTask.progress?.head || '尚未读取' }}</p><p>接收时间：{{ fmt(detailTask.created) }}</p><p>结束时间：{{ fmt(detailTask.progress?.finished_at) }}</p><p>阶段：{{ detailTask.stage }}</p><p class="task-message">{{ detailTask.error }}</p></template></el-dialog>
  </el-drawer>
</template>
<script setup>
import { computed, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import { attentionRoute, attentionWait, refreshReviewAttention } from '../composables/useReviewAttention'
const props = defineProps({ write: { type: Function, required: true } })
const emit = defineEmits(['changed'])
const route = useRoute(), router = useRouter()
const repository = computed(() => Number(route.query.repository)), number = computed(() => Number(route.query.number))
const selected = computed(() => Number.isSafeInteger(repository.value) && repository.value > 0 && Number.isSafeInteger(number.value) && number.value > 0)
const visible = computed({ get: () => route.query.attention === '1', set: value => { if (!value) { const query = { ...route.query }; delete query.attention; delete query.repository; delete query.number; router.replace({ query }) } } })
const page = ref(1), pendingPage = ref(1), waitingPage = ref(1), items = ref([]), total = ref(0)
const pendingTasks = ref([]), waitingTasks = ref([]), pendingTotal = ref(0), waitingTotal = ref(0), group = ref(null), worker = ref(null)
const loading = ref(false), loaded = ref(false), error = ref(''), actionBusy = reactive({}), actionErrors = reactive({}), detailTask = ref(null)
const detailVisible = computed({ get: () => !!detailTask.value, set: v => { if (!v) detailTask.value = null } })
let generation = 0, timer = null, busyGeneration = null, seen = new Set()
const fmt = value => value ? new Date(value * 1000).toLocaleString('zh-CN') : '尚无'
const message = e => e.response?.data?.error || '核对请求失败，请检查服务状态'
function reconcileLabel(task) { return ({ queued: '等待执行核对', checking: '核对中', needs_confirmation: '需要人工确认', failed: '核对失败', resolved: '已解决' })[task.reconciliation?.state] || '需要人工确认' }

async function requestCheck(task, current) {
  actionBusy[task.id] = true; delete actionErrors[task.id]
  try { await props.write('post', `/tasks/${task.id}/reconcile`, {}) }
  catch (e) { if (current === generation) actionErrors[task.id] = message(e) }
  finally { if (current === generation) actionBusy[task.id] = false }
}
async function load(auto = false) {
  if (!visible.value || busyGeneration === generation) return
  const current = generation
  let reloadPage = false
  busyGeneration = current; loading.value = true
  try {
    const params = selected.value ? { repository: repository.value, number: number.value } : { page: page.value }
    const requests = [api.get('/ai-review/attention', { params, timeout: 5000 })]
    if (selected.value) requests.push(api.get('/ai-review/tasks', { params: { ...params, status: 'publish_unknown', page: pendingPage.value }, timeout: 5000 }), api.get('/ai-review/tasks', { params: { ...params, status: 'queued', page: waitingPage.value }, timeout: 5000 }))
    const results = await Promise.all(requests)
    if (current !== generation) return
    const value = results[0].data
    worker.value = value.worker; items.value = value.items; total.value = value.total
    if (selected.value) {
      group.value = value.items[0] || group.value
      pendingTasks.value = results[1].data.items; pendingTotal.value = results[1].data.total
      waitingTasks.value = results[2].data.items; waitingTotal.value = results[2].data.total
      if (pendingPage.value > Math.max(1, Math.ceil(pendingTotal.value / 20))) { pendingPage.value = Math.max(1, Math.ceil(pendingTotal.value / 20)); reloadPage = true }
      if (waitingPage.value > Math.max(1, Math.ceil(waitingTotal.value / 20))) { waitingPage.value = Math.max(1, Math.ceil(waitingTotal.value / 20)); reloadPage = true }
    } else if (page.value > Math.max(1, Math.ceil(total.value / 20))) { page.value = Math.max(1, Math.ceil(total.value / 20)); reloadPage = true }
    loaded.value = true; error.value = ''
    if (auto && selected.value) {
      for (const task of pendingTasks.value) {
        if (current !== generation) break
        if (seen.has(task.id)) continue
        seen.add(task.id)
        if (!['queued', 'checking'].includes(task.reconciliation?.state)) await requestCheck(task, current)
      }
    }
  } catch (e) { if (current === generation) error.value = message(e) }
  finally { if (current === generation) { loading.value = false; busyGeneration = null } }
  if (reloadPage && current === generation) await load(auto)
}
async function check(task) {
  const current = generation
  await requestCheck(task, current)
  if (current !== generation) return
  await Promise.all([load(), refreshReviewAttention(true)])
  emit('changed')
}
async function confirm(task) {
  const current = generation
  try {
    await ElMessageBox.confirm('请先在 Gitea 核对：原请求已经结束、没有本任务的正式 Review，并已处理机器人残留草稿。确认后系统会再次读取远端结果，符合条件才解除阻塞。', '确认原任务未发布', { confirmButtonText: '已核对，确认未发布', cancelButtonText: '返回核对', type: 'warning' })
    if (current !== generation) return
    actionBusy[task.id] = true; delete actionErrors[task.id]
    await props.write('post', `/tasks/${task.id}/reconcile`, { confirm_unpublished: true })
    if (current !== generation) return
    ElMessage.success('任务核对结果已更新')
    pendingPage.value = 1
    await Promise.all([load(), refreshReviewAttention(true)]); emit('changed')
  } catch (e) { if (current === generation && e !== 'cancel' && e !== 'close') actionErrors[task.id] = message(e) }
  finally { if (current === generation) actionBusy[task.id] = false }
}
async function tick(current) {
  if (current !== generation || !visible.value) return
  if (!document.hidden) await load()
  if (current === generation && visible.value) timer = setTimeout(() => tick(current), 5000)
}
function onVisible() { if (!document.hidden && visible.value) load() }
watch(() => [route.query.attention, route.query.repository, route.query.number], async () => {
  const current = ++generation; clearTimeout(timer); seen = new Set()
  page.value = pendingPage.value = waitingPage.value = 1; loaded.value = false; error.value = ''; group.value = null
  items.value = []; pendingTasks.value = []; waitingTasks.value = []; pendingTotal.value = waitingTotal.value = 0; detailTask.value = null
  for (const key of Object.keys(actionErrors)) delete actionErrors[key]
  for (const key of Object.keys(actionBusy)) delete actionBusy[key]
  if (!visible.value) return
  await load(true)
  if (current === generation) { await load(); if (current === generation) timer = setTimeout(() => tick(current), 5000) }
}, { immediate: true })
document.addEventListener('visibilitychange', onVisible)
onUnmounted(() => { generation++; clearTimeout(timer); document.removeEventListener('visibilitychange', onVisible) })
</script>
<style scoped>
.drawer-heading, .task-heading, .task-actions, .task-times, .waiting-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; min-width: 0; }
.drawer-heading, .task-heading { justify-content: space-between; }.drawer-heading h3 { overflow-wrap: anywhere; font-size: 18px; }.drawer-heading p, .drawer-help, .task-id, .task-times { color: var(--text-secondary); font-size: 13px; line-height: 1.8; margin: 10px 0; }
.drawer-heading, .drawer-help { margin-bottom: 20px; }.pending-task { padding: 20px 0; border-bottom: 1px solid #e4e9ef; }.task-heading strong { min-width: 0; overflow-wrap: anywhere; }.task-id, .task-message { overflow-wrap: anywhere; }.task-message { line-height: 1.8; margin: 12px 0; }.task-actions { margin-top: 16px; }.task-actions :deep(.el-button) { margin-left: 0; }.waiting-tasks { margin-top: 28px; }.waiting-row { padding: 12px 0; border-bottom: 1px solid #edf0f4; font-size: 13px; }.waiting-row span { color: var(--text-secondary); }:deep(.el-alert) { margin-bottom: 16px; }:deep(.el-pagination) { margin-top: 18px; flex-wrap: wrap; }
</style>
