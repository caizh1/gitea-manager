<template>
  <section class="setup-panel" :class="embedded ? 'setup-embedded' : 'glass-card'" aria-labelledby="setup-title">
    <div class="setup-heading"><div><h4 id="setup-title">快速接入 AI 审查</h4><p>选择一个 Gitea 实例及其仓库，系统准备机器人、事件通知和 PR 进度。</p></div><el-button v-if="configured && !embedded" text @click="expanded = !expanded">{{ expanded ? '收起配置' : '接入更多仓库' }}</el-button></div>
    <el-alert v-if="error || catalogError || pollError" :title="error || catalogError || pollError" type="error" :closable="false" show-icon />
    <el-alert v-if="!available" title="当前部署尚未启用自动接入，请先运行一键升级脚本。" type="info" :closable="false" />
    <el-form v-if="embedded || expanded || !configured || job?.detail?.needs_callback" label-position="top" @submit.prevent="submit">
      <div v-if="savedModel" class="model-summary"><strong>使用已保存的模型</strong><span>{{ config.model }}</span><span>{{ config.api_base_url }}</span><small>修改模型请到“模型与设置”；继续检查会使用最新保存配置。</small></div>
      <div v-else class="setup-grid">
        <el-form-item label="内网模型地址"><el-input v-model="form.api_base_url" aria-label="内网模型地址" placeholder="包含 API 前缀，例如 https://模型服务/v1" :disabled="locked" /></el-form-item>
        <el-form-item label="API Key"><el-input v-model="form.api_key" aria-label="模型 API Key" type="password" autocomplete="new-password" :disabled="locked" /></el-form-item>
        <el-form-item label="模型名称"><el-input v-model="form.model" aria-label="模型名称" :disabled="locked" /></el-form-item>
      </div>
      <div class="setup-grid">
        <el-form-item label="Gitea 实例"><el-select v-model="serverId" aria-label="Gitea 实例" class="full" :disabled="locked" @change="changeServer"><el-option v-for="s in servers" :key="s.id" :value="s.id" :label="s.name" /></el-select></el-form-item>
        <el-form-item label="选择需要启用的仓库"><el-select v-model="selected" aria-label="选择需要启用的仓库" class="full" multiple filterable remote reserve-keyword :remote-method="search" :loading="searching" :disabled="locked || !serverId" placeholder="搜索并选择当前实例的仓库"><el-option v-for="item in options" :key="item.key" :value="item.key" :label="item.label" /></el-select></el-form-item>
      </div>
      <el-form-item v-if="job?.detail?.needs_callback" label="Gitea 可访问的 Manager 地址"><el-input v-model="form.manager_url" aria-label="Manager 回调地址" placeholder="填写 Gitea 服务器能够访问的 Manager 地址" :disabled="locked" /></el-form-item>
      <div class="setup-actions"><el-button type="primary" native-type="submit" :loading="locked" :disabled="!available || !selected.length || locked">检查并启用</el-button><span class="setup-note">本次目标：{{ serverName || '尚未选择实例' }}，{{ selected.length }} 个仓库。</span></div>
    </el-form>
    <div v-if="job" class="setup-progress" aria-live="polite">
      <div class="setup-actions"><strong>{{ job.server_name || serverName }}</strong><el-tag :type="job.status === 'succeeded' ? 'success' : running ? 'primary' : 'warning'">{{ running ? '正在接入' : job.status === 'succeeded' ? '接入完成' : '需要处理' }}</el-tag><span>{{ job.detail?.stage }}</span><el-button v-if="['failed','partial'].includes(job.status) && !job.detail?.needs_callback" text type="primary" :loading="submitting" :disabled="locked" @click="resume">继续检查</el-button></div>
      <p class="setup-note">任务使用模型：{{ job.model }} · 检查轮次 {{ job.detail?.attempt || 1 }}</p>
      <el-alert v-if="job.detail?.error" :title="job.detail.error" type="error" :closable="false" />
      <ul v-if="job.detail?.results?.length"><li v-for="r in job.detail.results" :key="`${job.server_id}:${r.name}`"><strong>{{ r.name }}</strong> · {{ r.ok ? '已启用' : r.error || '未执行' }}<span v-if="!r.ok && r.step"> · {{ r.step }}<template v-if="r.error_code">（{{ r.error_code }}）</template></span></li></ul>
      <p v-else-if="job.status === 'failed'" class="setup-note">本轮在仓库操作前停止；容器、机器人及 Webhook 检查尚未执行。</p>
      <details v-if="job.detail?.history?.length"><summary>以前的检查记录（{{ job.detail.history.length }}）</summary><div v-for="(h, i) in job.detail.history" :key="i"><p>{{ h.stage }} · {{ h.error || '已保存阶段结果' }}</p><p v-for="r in h.results || []" :key="r.name">{{ r.name }}：{{ r.ok ? '当时检查通过' : r.error }}</p></div></details>
    </div>
  </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '../api'
import { errorMessage } from './reviewDisplay'
const props = defineProps({ servers: { type: Array, default: () => [] }, config: { type: Object, default: () => ({}) }, repositories: { type: Array, default: () => [] }, embedded: Boolean })
const emit = defineEmits(['changed'])
const form = ref({ api_base_url: '', api_key: '', model: 'deepseek-v4-flash', manager_url: '' })
const serverId = ref(null), selected = ref([]), options = ref([]), available = ref(true), job = ref(null), error = ref('')
const catalogError = ref(''), pollError = ref('')
const expanded = ref(false), searching = ref(false), submitting = ref(false)
const configured = computed(() => props.repositories.length > 0)
const savedModel = computed(() => props.config.api_key_configured && props.config.api_base_url && props.config.model)
const running = computed(() => ['pending', 'running'].includes(job.value?.status))
const locked = computed(() => running.value || submitting.value)
const serverName = computed(() => props.servers.find(s => s.id === serverId.value)?.name || '')
let timer, stopped = false, generation = 0, statusGeneration = 0
watch(() => props.servers, rows => { if (!serverId.value && rows.length) { serverId.value = rows[0].id; changeServer() } }, { immediate: true })
async function write(path, data) { const token = (await api.get('/ai-review/csrf')).data.token; return api.post('/ai-review' + path, data, { headers: { 'X-Review-CSRF': token } }) }
function changeServer() { generation++; statusGeneration++; selected.value = []; options.value = []; job.value = null; error.value = ''; catalogError.value = ''; pollError.value = ''; form.value.manager_url = ''; search('') }
async function search(q) {
  if (!serverId.value || stopped) return
  const id = serverId.value, current = ++generation; searching.value = true; catalogError.value = ''
  try {
    const result = await api.get('/ai-review/catalog', { params: { server_id: id, q } })
    if (stopped || current !== generation || id !== serverId.value) return
    const preserved = options.value.filter(r => r.server_id === id && selected.value.includes(r.key))
    const rows = result.data.items.map(r => ({ key: `${id}:${r.id}`, label: `${serverName.value} / ${r.name}`, server_id: id, repo_id: r.id, name: r.name }))
    options.value = [...new Map([...preserved, ...rows].map(r => [r.key, r])).values()]
  } catch (e) { if (!stopped && current === generation) catalogError.value = errorMessage(e) }
  finally { if (current === generation) searching.value = false }
}
async function submit() {
  if (locked.value || stopped) return
  error.value = ''; submitting.value = true; const current = ++statusGeneration, id = serverId.value
  try {
    const repositories = selected.value.map(key => options.value.find(r => r.key === key))
    if (!repositories.length || repositories.some(r => !r || r.server_id !== id)) throw new Error('仓库选择已变化，请重新选择')
    const payload = { server_id: id, config_revision: props.config.revision, repositories: repositories.map(({ server_id, repo_id, name }) => ({ server_id, repo_id, name })) }
    if (!savedModel.value) Object.assign(payload, form.value)
    if (form.value.manager_url) payload.manager_url = form.value.manager_url
    if (!payload.api_key) delete payload.api_key
    if (!payload.manager_url) delete payload.manager_url
    const response = await write('/setup', payload)
    if (!stopped && current === statusGeneration && id === serverId.value) { job.value = response.data.job; form.value.api_key = '' }
  } catch (e) { if (!stopped && current === statusGeneration) error.value = e.response ? errorMessage(e) : e.message || errorMessage(e) }
  finally { if (current === statusGeneration) submitting.value = false }
}
async function resume() {
  if (locked.value || !job.value || job.value.server_id !== serverId.value) return
  error.value = ''; submitting.value = true; const current = ++statusGeneration
  try {
    const response = await write(`/setup/${job.value.id}/resume`, { server_id: serverId.value, config_revision: props.config.revision })
    if (!stopped && current === statusGeneration) job.value = response.data.job
  } catch (e) { if (!stopped && current === statusGeneration) error.value = errorMessage(e) }
  finally { if (current === statusGeneration) submitting.value = false }
}
async function poll() {
  if (stopped) return
  const current = statusGeneration, id = serverId.value, jobId = job.value?.id
  try {
    if (id && !submitting.value) {
      const previous = job.value?.status
      const r = await api.get(jobId ? `/ai-review/setup/${jobId}` : '/ai-review/setup', { notifyError: false, params: jobId ? undefined : { server_id: id } })
      if (!stopped && current === statusGeneration && id === serverId.value) {
        pollError.value = ''
        if ('automatic_available' in r.data) available.value = r.data.automatic_available
        if (!r.data.job || r.data.job.server_id === id) job.value = r.data.job
        if (['pending', 'running'].includes(previous) && !running.value) emit('changed')
      }
    }
  } catch (e) { if (!stopped && current === statusGeneration) pollError.value = errorMessage(e) }
  if (!stopped) timer = setTimeout(poll, running.value ? 2000 : 5000)
}
onMounted(poll)
onUnmounted(() => { stopped = true; generation++; statusGeneration++; clearTimeout(timer); form.value.api_key = '' })
</script>

<style scoped>
.setup-panel { padding: 22px; margin-bottom: 20px; min-width: 0; }
.setup-panel.setup-embedded { padding: 0; }
.setup-embedded .setup-grid { grid-template-columns: minmax(0, 1fr); }
.setup-heading, .setup-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; min-width: 0; }
.setup-heading { justify-content: space-between; margin-bottom: 16px; }
h4 { margin: 0 0 8px; font-size: 17px; }
.setup-heading p, .setup-note { color: var(--text-secondary, #64748b); font-size: 13px; line-height: 1.6; margin: 0; }
.setup-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 22px; }
.full { width: 100%; min-width: 0; }
.model-summary { display: flex; flex-wrap: wrap; gap: 8px 16px; padding: 14px; background: var(--bg-hover, #f5f7fa); border-radius: 10px; margin-bottom: 18px; overflow-wrap: anywhere; }
.model-summary small { flex-basis: 100%; color: var(--text-secondary, #64748b); }
details { margin: 14px 0; } summary { cursor: pointer; margin-bottom: 12px; }
.setup-progress { margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--border-color, #e2e8f0); overflow-wrap: anywhere; }
.setup-progress li { margin: 8px 0; line-height: 1.6; }
.el-alert { margin-bottom: 16px; }
@media (max-width: 720px) { .setup-grid { grid-template-columns: minmax(0, 1fr); } .setup-panel { padding: 14px; } }
</style>
