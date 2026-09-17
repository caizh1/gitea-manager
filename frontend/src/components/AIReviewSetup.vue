<template>
  <section class="glass-card setup-panel" aria-labelledby="setup-title">
    <div class="setup-heading"><div><h4 id="setup-title">快速接入 AI 审查</h4><p>配置一次，后续升级自动保留。机器人、事件通知和 PR 进度由系统准备。</p></div><el-button v-if="configured" text @click="expanded = !expanded">{{ expanded ? '收起配置' : '接入更多仓库' }}</el-button></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <el-alert v-if="!available" title="当前部署尚未启用自动接入，请先运行一键升级脚本。" type="info" :closable="false" />
    <el-form v-if="expanded || !configured || job?.detail?.needs_callback" label-position="top" @submit.prevent="submit">
      <div class="setup-grid">
        <el-form-item label="内网模型地址"><el-input v-model="form.api_base_url" aria-label="内网模型地址" placeholder="包含 API 前缀，例如 https://模型服务/v1" :disabled="running" /></el-form-item>
        <el-form-item :label="hasKey ? 'API Key（留空沿用已保存的 Key）' : 'API Key'"><el-input v-model="form.api_key" aria-label="模型 API Key" type="password" autocomplete="new-password" :disabled="running" /></el-form-item>
        <el-form-item label="Gitea 实例"><el-select v-model="serverId" aria-label="Gitea 实例" class="full" :disabled="running" @change="search('')"><el-option v-for="s in servers" :key="s.id" :value="s.id" :label="s.name" /></el-select></el-form-item>
        <el-form-item label="选择需要启用的仓库"><el-select v-model="selected" aria-label="选择需要启用的仓库" class="full" multiple filterable remote reserve-keyword :remote-method="search" :loading="searching" :disabled="running || !serverId" placeholder="可搜索并选择多个仓库"><el-option v-for="item in options" :key="item.key" :value="item.key" :label="item.label" /></el-select></el-form-item>
      </div>
      <el-form-item v-if="job?.detail?.needs_callback" label="Gitea 可访问的 Manager 地址"><el-input v-model="form.manager_url" aria-label="Manager 回调地址" placeholder="填写 Gitea 服务器能够访问的 Manager 地址" /><span class="setup-note">系统无法从 Gitea 回连当前入口，仅需修正这一个地址。</span></el-form-item>
      <details><summary>高级设置</summary><el-form-item label="模型名称"><el-input v-model="form.model" aria-label="模型名称" :disabled="running" /></el-form-item><p class="setup-note">其余参数沿用现有设置；默认使用 OpenCode CLI。</p></details>
      <div class="setup-actions"><el-button type="primary" native-type="submit" :loading="running || submitting" :disabled="!available || !selected.length">检查并启用</el-button><span class="setup-note">仅启用本次选择且检查通过的仓库。</span></div>
    </el-form>
    <div v-if="job" class="setup-progress" aria-live="polite">
      <div class="setup-actions"><el-tag :type="job.status === 'succeeded' ? 'success' : running ? 'primary' : 'warning'">{{ running ? '正在接入' : job.status === 'succeeded' ? '接入完成' : '需要处理' }}</el-tag><span>{{ job.detail.stage }}</span><el-button v-if="['failed','partial'].includes(job.status) && !job.detail.needs_callback" text type="primary" @click="resume">继续检查</el-button></div>
      <p v-if="job.detail.error" class="setup-error">{{ job.detail.error }}</p>
      <ul v-if="job.detail.results?.length"><li v-for="r in job.detail.results" :key="r.name"><strong>{{ r.name }}</strong> · {{ r.ok ? '已启用' : r.error }}</li></ul>
    </div>
  </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '../api'
const props = defineProps({ servers: { type: Array, default: () => [] }, config: { type: Object, default: () => ({}) }, repositories: { type: Array, default: () => [] } })
const emit = defineEmits(['changed'])
const form = ref({ api_base_url: '', api_key: '', model: 'deepseek-v4-flash', manager_url: '' })
const serverId = ref(null), selected = ref([]), options = ref([]), available = ref(true), job = ref(null), error = ref('')
const expanded = ref(false), searching = ref(false), submitting = ref(false)
const configured = computed(() => props.repositories.length > 0), hasKey = computed(() => props.config.api_key_configured)
const running = computed(() => ['pending', 'running'].includes(job.value?.status))
let timer, stopped = false, generation = 0, initialized = false
watch(() => props.config, c => { if (!initialized && 'api_base_url' in c) { form.value.api_base_url = c.api_base_url; form.value.model = c.model || 'deepseek-v4-flash'; initialized = true } }, { immediate: true })
watch(() => props.servers, rows => { if (!serverId.value && rows.length) { serverId.value = rows[0].id; search('') } }, { immediate: true })
function failure(e) { error.value = e.response?.data?.error || '接入请求未完成，请检查服务状态' }
async function write(path, data) { const token = (await api.get('/ai-review/csrf')).data.token; return api.post('/ai-review' + path, data, { headers: { 'X-Review-CSRF': token } }) }
async function search(q) {
  if (!serverId.value) return
  const id = serverId.value, current = ++generation; searching.value = true
  try { const result = await api.get('/ai-review/catalog', { params: { server_id: id, q } }); if (current !== generation) return
    const preserved = options.value.filter(r => selected.value.includes(r.key))
    const rows = result.data.items.map(r => ({ key: `${id}:${r.id}`, label: r.name, server_id: id, repo_id: r.id, name: r.name }))
    options.value = [...new Map([...preserved, ...rows].map(r => [r.key, r])).values()]
  } catch (e) { failure(e) } finally { if (current === generation) searching.value = false }
}
async function submit() {
  error.value = ''; submitting.value = true
  try { const repositories = selected.value.map(key => options.value.find(r => r.key === key)).filter(Boolean).map(({ server_id, repo_id, name }) => ({ server_id, repo_id, name }))
    const payload = { ...form.value, repositories }; if (!payload.api_key) delete payload.api_key; if (!payload.manager_url) delete payload.manager_url
    job.value = (await write('/setup', payload)).data.job; form.value.api_key = ''
  } catch (e) { failure(e) } finally { submitting.value = false }
}
async function resume() { error.value = ''; try { job.value = (await write(`/setup/${job.value.id}/resume`, {})).data.job } catch (e) { failure(e) } }
async function poll() {
  try { const previous = job.value?.status; const r = await api.get('/ai-review/setup'); available.value = r.data.automatic_available; job.value = r.data.job
    if (['pending', 'running'].includes(previous) && !running.value) emit('changed')
  } catch (e) { failure(e) }
  if (!stopped) timer = setTimeout(poll, running.value ? 2000 : 5000)
}
onMounted(poll)
onUnmounted(() => { stopped = true; generation++; clearTimeout(timer); form.value.api_key = '' })
</script>

<style scoped>
.setup-panel { padding: 22px; margin-bottom: 20px; min-width: 0; }
.setup-heading, .setup-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; min-width: 0; }
.setup-heading { justify-content: space-between; margin-bottom: 16px; }
h4 { margin: 0 0 8px; font-size: 17px; }
.setup-heading p, .setup-note { color: var(--text-secondary, #64748b); font-size: 13px; line-height: 1.6; margin: 0; }
.setup-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 22px; }
.full { width: 100%; min-width: 0; }
details { margin-bottom: 16px; } summary { cursor: pointer; margin-bottom: 12px; }
.setup-progress { margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--border-color, #e2e8f0); overflow-wrap: anywhere; }
.setup-progress li { margin: 8px 0; line-height: 1.6; }
.setup-error { color: #b45309; }
.el-alert { margin-bottom: 16px; }
@media (max-width: 720px) { .setup-grid { grid-template-columns: minmax(0, 1fr); } .setup-panel { padding: 14px; } }
</style>
