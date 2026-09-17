<template>
  <div class="review-page">
    <div class="section-header review-toolbar">
      <div><h3 class="section-title">AI Code Review</h3><p class="muted">自动分析 PR 变更，提供可追溯的辅助审查意见。</p></div>
      <div class="review-actions"><el-tag :type="config.enabled ? 'success' : 'info'">{{ config.enabled ? '已启用' : '全局关闭' }}</el-tag><el-button :icon="Refresh" @click="refresh">刷新</el-button></div>
    </div>
    <el-alert v-if="error" :title="error" type="error" show-icon closable @close="error = ''" />
    <ReviewAttentionPanel @open="openAttention" />
    <el-alert v-if="workerStale" title="Review 工作进程尚未就绪或心跳已过期，自动审查可能暂停。" type="warning" show-icon :closable="false" />
    <el-alert v-if="progressStale" title="PR 进度同步进程尚未就绪或心跳已过期，审查仍可执行，但 Gitea 页面状态可能延迟。" type="warning" show-icon :closable="false" />
    <el-alert v-if="feedbackStale" title="人工反馈同步进程尚未就绪或心跳已过期，样本来源超过有效期后将暂停参考；自动审查仍可执行。" type="warning" show-icon :closable="false" />
    <el-tabs v-model="tab">
      <el-tab-pane label="审查任务" name="tasks">
        <div class="glass-card panel">
          <div class="review-toolbar"><div class="review-actions"><h4 class="task-record-title">任务记录</h4><el-select v-model="filterRepo" clearable placeholder="全部仓库" aria-label="筛选仓库" class="selector" @change="page = 1; loadTasks()"><el-option v-for="r in repositories" :key="r.id" :value="r.id" :label="r.repo_name" /></el-select></div><span class="muted">共 {{ total }} 条</span></div>
          <el-table :data="tasks" stripe max-height="var(--table-max-height)">
            <el-table-column label="仓库 / PR" min-width="230"><template #default="{ row }">{{ repoName(row.repository) }} #{{ row.number }}</template></el-table-column>
            <el-table-column label="代码版本" min-width="135"><template #default="{ row }"><code>{{ row.progress?.head?.slice(0, 12) || '等待读取' }}</code></template></el-table-column>
            <el-table-column label="状态" min-width="135"><template #default="{ row }"><el-tag :type="row.status === 'queued' && row.attention?.required ? 'danger' : statusType(row.status)">{{ row.progress?.label || statusLabel(row.status) }}</el-tag></template></el-table-column>
            <el-table-column label="当前阶段" min-width="230"><template #default="{ row }">{{ row.progress?.detail || row.stage }}<div v-if="row.progress?.units_total != null" class="muted">完成单元 {{ row.progress.units_completed }}/{{ row.progress.units_total }}</div><div v-if="row.progress?.sync_error" class="failure">状态同步：{{ row.progress.sync_error }}</div></template></el-table-column>
            <el-table-column label="时间" min-width="185"><template #default="{ row }">{{ fmt(row.updated) }}</template></el-table-column>
            <el-table-column label="操作" min-width="170"><template #default="{ row }"><div class="review-actions"><el-button link type="primary" @click="openTask(row.id)">详情</el-button><el-button v-if="row.status === 'publish_unknown' || (row.status === 'queued' && row.attention?.required)" link type="danger" @click="openAttention(row)">{{ row.status === 'publish_unknown' ? '立即处理' : '查看阻塞' }}</el-button><el-button v-else link :disabled="!config.enabled" @click="rerun(row.repository, row.number)">重新审查</el-button></div></template></el-table-column>
            <template #empty><el-empty description="启用仓库后，创建或更新 PR 即可自动审查。" /></template>
          </el-table>
          <el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next, total" @current-change="loadTasks" />
        </div>
      </el-tab-pane>
      <el-tab-pane label="仓库管理" name="repositories">
    <AIReviewSetup :servers="servers" :config="config" :repositories="repositories" @changed="refresh" />
        <div class="glass-card panel">
          <div class="review-toolbar"><span>为仓库配置只读机器人，并安装 PR 事件通知。</span><el-button type="primary" :icon="Plus" @click="editRepo()">添加仓库</el-button></div>
          <el-table :data="repositories" stripe max-height="var(--table-max-height)">
            <el-table-column label="Gitea 实例" min-width="180"><template #default="{ row }">{{ row.server_name }}<div class="muted">{{ row.gitea_version || '请刷新仓库配置以核对版本' }}</div></template></el-table-column>
            <el-table-column prop="repo_name" label="仓库" min-width="220" />
            <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '关闭' }}</el-tag></template></el-table-column>
            <el-table-column prop="bot_name" label="审查机器人" min-width="150" />
            <el-table-column label="事件通知" min-width="230"><template #default="{ row }"><div>{{ row.hook_status }}</div><small class="muted">最近收到：{{ fmt(row.last_event) }}</small><div v-if="row.reconcile_error" class="failure">{{ row.reconcile_error }}</div></template></el-table-column>
            <el-table-column label="操作" min-width="200"><template #default="{ row }"><div class="review-actions"><el-button link type="primary" @click="editRepo(row)">配置</el-button><el-button link @click="downloadProgress(row.server_id)">下载 PR 进度组件</el-button><el-button link @click="hookRepo = row; hookVisible = true">事件通知</el-button><el-button link :disabled="!row.enabled || !config.enabled" @click="manualRepo = row; manualVisible = true">审查 PR</el-button></div></template></el-table-column>
          </el-table>
        </div>
      </el-tab-pane>
      <el-tab-pane label="审查经验数据集" name="dataset"><AIReviewDataset :repositories="repositories" /></el-tab-pane>
      <el-tab-pane label="模型与设置" name="config">
    <el-alert title="本地校验后直接发布行内评论；无法可靠定位的问题保留为普通意见。无需 Gitea 补丁。" type="info" show-icon :closable="false" />

        <div class="glass-card panel">
          <el-form label-position="top" @submit.prevent="saveConfig">
            <div class="review-grid">
              <el-form-item label="全局启用"><el-switch v-model="config.enabled" aria-label="全局启用 AI Review" /></el-form-item>
              <el-form-item label="执行引擎"><el-select v-model="config.engine" aria-label="执行引擎"><el-option label="OpenCode CLI 1.18.7" value="opencode_cli" /><el-option label="New API HTTP" value="new_api_http" /></el-select></el-form-item>
              <el-form-item label="内部 New API 地址"><el-input v-model="config.api_base_url" placeholder="填写包含 API 前缀的内网地址" autocomplete="off" /></el-form-item>
              <el-form-item label="模型"><el-input v-model="config.model" placeholder="填写内部实际可用的模型标识" autocomplete="off" /></el-form-item>
              <el-form-item :label="config.api_key_configured ? 'API Key（已配置，留空保留）' : 'API Key（尚未配置）'"><el-input v-model="apiKey" type="password" autocomplete="new-password" placeholder="仅在新增或更换密钥时填写" /></el-form-item>
              <el-form-item label="连续 Push 合并窗口（秒）"><el-input-number v-model="config.debounce_seconds" :min="0" :max="120" /></el-form-item>
            </div>
            <el-collapse><el-collapse-item title="审查资源预算" name="limits"><div class="review-grid">
              <el-form-item label="每次最多文件数"><el-input-number v-model="config.max_files" :min="1" :max="100" /></el-form-item>
              <el-form-item label="每次最多模型调用"><el-input-number v-model="config.max_calls" :min="2" :max="64" /></el-form-item>
              <el-form-item label="任务时间上限（秒）"><el-input-number v-model="config.task_timeout_seconds" :min="60" :max="3600" /></el-form-item>
              <el-form-item label="每次最多意见数"><el-input-number v-model="config.max_findings" :min="1" :max="30" /></el-form-item>
            </div><p class="muted">达到预算会明确标记未覆盖范围，不会给出整个 PR 无问题的结论。</p></el-collapse-item></el-collapse>
            <div class="review-actions footer"><el-button type="primary" :loading="busy" native-type="submit">保存设置</el-button><el-button :loading="testing" @click="testConnection">检查已保存的连接</el-button></div>
          </el-form>
          <el-alert v-if="connectionMessage" :title="connectionMessage" :type="connectionOk ? 'success' : 'warning'" :closable="false" />
        </div>
      </el-tab-pane>
    </el-tabs>
    <ReviewAttentionDrawer :write="write" @changed="loadTasks" />

    <el-dialog v-model="repoVisible" :title="repoForm.id ? '配置审查仓库' : '添加审查仓库'" width="min(640px, 95vw)" :close-on-click-modal="false">
      <el-form label-position="top">
        <el-form-item label="Gitea 实例"><el-select v-model="repoForm.server_id" :disabled="!!repoForm.id" class="full" @change="catalog = []; repoForm.repo_name = ''; searchCatalog('')"><el-option v-for="s in servers" :key="s.id" :value="s.id" :label="s.name" /></el-select></el-form-item>
        <el-form-item label="仓库"><el-select v-model="repoForm.repo_name" :disabled="!!repoForm.id" filterable remote :remote-method="searchCatalog" :loading="catalogLoading" placeholder="搜索所有者或仓库名称" class="full"><el-option v-for="r in catalog" :key="r.id" :value="r.name" :label="r.name" /><el-option v-if="repoForm.id" :label="repoForm.repo_name" :value="repoForm.repo_name" /></el-select></el-form-item>
        <el-form-item label="专用机器人 Token（编辑时留空保留）"><el-input v-model="repoForm.bot_token" type="password" autocomplete="new-password" /></el-form-item>
        <p class="muted">机器人使用独立非管理员账号，并作为仓库只读成员；Token 配置 read:user、write:repository、write:issue 以读取代码并发布审查和总体评论。系统会拒绝拥有代码写入权限的机器人。</p>
        <el-form-item label="启用该仓库"><el-switch v-model="repoForm.enabled" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="repoVisible = false">取消</el-button><el-button type="primary" :loading="busy" @click="saveRepo">保存仓库</el-button></template>
    </el-dialog>
    <el-dialog v-model="hookVisible" title="安装或核对 PR 事件通知" width="min(600px, 95vw)">
      <p>{{ hookRepo?.repo_name }}</p><el-form label-position="top"><el-form-item label="Gitea 能够访问的 Manager 地址"><el-input v-model="managerURL" placeholder="填写 Manager 完整访问地址" /></el-form-item></el-form>
      <p class="muted">将使用现有服务器管理凭据安装 Webhook，并回读验证。该地址必须在部署白名单内。</p>
      <template #footer><el-button @click="hookVisible = false">取消</el-button><el-button type="primary" :loading="busy" @click="installHook">安装并验证</el-button></template>
    </el-dialog>
    <el-dialog v-model="manualVisible" title="人工触发 PR 审查" width="min(460px, 95vw)"><p>{{ manualRepo?.repo_name }}</p><el-form label-position="top"><el-form-item label="PR 编号"><el-input-number v-model="manualNumber" :min="1" /></el-form-item></el-form><template #footer><el-button @click="manualVisible = false">取消</el-button><el-button type="primary" @click="rerun(manualRepo.id, manualNumber)">开始审查</el-button></template></el-dialog>
    <el-drawer v-model="detailVisible" title="审查任务详情" size="min(900px, 96vw)">
      <template v-if="detail">
        <div class="review-toolbar"><strong>{{ repoName(detail.repository) }} #{{ detail.number }}</strong><el-tag :type="statusType(detail.status)">{{ detail.progress?.label || statusLabel(detail.status) }}</el-tag></div>
        <p>{{ detail.stage }}</p>
        <template v-if="detail.progress"><p>接收：{{ fmt(detail.progress.received_at) }}；开始：{{ fmt(detail.progress.started_at) }}；结束：{{ fmt(detail.progress.finished_at) }}</p><p v-if="detail.progress.started_at">已用时：{{ detail.progress.elapsed_seconds }} 秒</p><p v-if="detail.progress.units_total != null">已完成分析单元：{{ detail.progress.units_completed }}/{{ detail.progress.units_total }}</p><el-alert v-if="detail.progress.sync_error" :title="'PR 状态同步：' + detail.progress.sync_error" type="warning" :closable="false" /></template><el-alert v-if="detail.error" :title="detail.error" :type="detail.status === 'succeeded' ? 'info' : 'warning'" :closable="false" />
        <p class="wrap">任务：{{ detail.id }}</p><p class="wrap">Head：{{ detail.progress?.head || '未读取' }}</p><p class="wrap">比较基点：{{ detail.snapshot?.merge_base || '未读取' }}</p>
        <p v-if="detail.configuration">执行配置版本 {{ detail.config_revision }}：{{ detail.configuration.engine }} · {{ detail.configuration.model }}</p>
        <template v-if="detail.result"><details v-if="detail.result.evidence?.some(e => e.input?.historical_samples?.length)"><summary>本次采用的历史参考</summary><template v-for="(e, i) in detail.result.evidence" :key="i"><p v-for="r in e.input?.historical_samples || []" :key="r.id">{{ e.path }} · {{ r.id }} · 修订 {{ r.revision }}：{{ r.correction }}</p></template></details><p>模型调用：{{ detail.model_calls }}；已读取文件：{{ detail.result.coverage.files_reviewed }}/{{ detail.result.coverage.files_total }}</p>
          <el-alert v-if="!detail.result.coverage.complete" title="本次只覆盖部分变更，请继续检查未覆盖内容。" type="warning" :closable="false" />
          <ul v-if="detail.result.coverage.skipped.length"><li v-for="(s, i) in detail.result.coverage.skipped" :key="i">{{ s.path }}：{{ s.reason }}</li></ul>
          <el-empty v-if="!detail.result.findings.length" :description="detail.status === 'succeeded' ? '在已覆盖的修改中未发现证据充分的明显问题。' : '本次审查尚未完成，不能给出无问题结论。'" />
          <article v-for="f in detail.result.findings" :key="f.candidate_id" class="finding"><div class="review-actions"><el-tag>{{ f.severity }}</el-tag><strong>{{ f.title }}</strong></div><p>{{ f.path }} · {{ f.scope }}</p><p v-if="f.finding_id">意见编号：{{ f.finding_id }}</p><el-alert v-if="f.assessment === 'uncertain'" title="待开发者确认：反证尚不充分，本条作为辅助建议。" type="warning" :closable="false" /><p v-if="f.anchor">{{ f.anchor.side === 'new' ? '新' : '旧' }}侧第 {{ f.anchor.line }} 行 <span v-if="f.existing_review_id">（已有历史 Review 意见，本次不重复）</span></p><p v-else class="muted">普通意见：{{ f.fallback_reason }}</p><pre v-if="f.quote">{{ f.quote }}</pre><p><strong>触发条件：</strong>{{ f.trigger }}</p><p><strong>原因：</strong>{{ f.explanation }}</p><p><strong>影响：</strong>{{ f.impact }}</p><p><strong>建议：</strong>{{ f.recommendation }}</p><p class="muted">反证检查：{{ f.verification }}</p><template v-if="f.quality_version >= 2"><p><strong>修改前：</strong>{{ f.before_behavior }}</p><p><strong>修改后：</strong>{{ f.after_behavior }}</p><ol><li v-for="(step, i) in f.trace" :key="i">{{ step }}</li></ol><details><summary>源码证据与位置核验</summary><div v-for="(source, i) in f.source_evidence" :key="i"><p>{{ source.version === 'before' ? '修改前' : '修改后' }} · {{ source.path }} · {{ source.role }}</p><pre>{{ source.quote }}</pre></div><p v-if="f.location_evidence">根因操作：{{ f.location_evidence.kind }}，第 {{ f.location_evidence.start }} 行</p></details></template></article>
        </template>
      </template>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { api } from '../api'
import AIReviewDataset from '../components/AIReviewDataset.vue'
import AIReviewSetup from '../components/AIReviewSetup.vue'
import ReviewAttentionPanel from '../components/ReviewAttentionPanel.vue'
import ReviewAttentionDrawer from '../components/ReviewAttentionDrawer.vue'
import { useRouter } from 'vue-router'
import { attentionRoute, refreshReviewAttention } from '../composables/useReviewAttention'
const router = useRouter()
function openAttention(item) { router.push(attentionRoute(item)) }

const tab = ref('tasks'), error = ref(''), busy = ref(false), testing = ref(false), csrf = ref('')
const config = ref({ enabled: false }), apiKey = ref(''), health = ref([]), servers = ref([]), repositories = ref([])
const tasks = ref([]), total = ref(0), page = ref(1), filterRepo = ref(null), detail = ref(null), detailVisible = ref(false)
const repoVisible = ref(false), repoForm = ref({}), catalog = ref([]), catalogLoading = ref(false)
const hookVisible = ref(false), hookRepo = ref(null), managerURL = ref(window.location.origin)
const manualVisible = ref(false), manualRepo = ref(null), manualNumber = ref(1)
const connectionMessage = ref(''), connectionOk = ref(false)
let timer, taskGeneration = 0, catalogGeneration = 0, disposed = false
const workerStale = computed(() => config.value.enabled && (!health.value.find(h => h.name === 'worker') || Date.now()/1000-health.value.find(h => h.name === 'worker').updated > 90))
const feedbackStale = computed(() => config.value.enabled && (!health.value.find(h => h.name === 'feedback') || Date.now()/1000-health.value.find(h => h.name === 'feedback').updated > 90))
const progressStale = computed(() => config.value.enabled && (!health.value.find(h => h.name === 'progress') || Date.now()/1000-health.value.find(h => h.name === 'progress').updated > 90))
const fmt = value => value ? new Date(value*1000).toLocaleString('zh-CN') : '尚无'
const repoName = id => repositories.value.find(r => r.id === id)?.repo_name || `仓库 ${id}`
const statusLabel = s => ({ queued: '等待执行', running: '分析中', publishing: '发布与核对', recovering: '核对发布结果', publish_unknown: '发布待核对', succeeded: '已完成', superseded: '版本已过期', failed: '执行失败' }[s] || s)
const statusType = s => s === 'succeeded' ? 'success' : ['failed', 'publish_unknown'].includes(s) ? 'warning' : s === 'superseded' ? 'info' : 'primary'
const showError = e => { error.value = e.response?.data?.error || '请求失败，请检查服务状态' }
async function downloadProgress(serverId) { try { const response = await api.get(`/ai-review/servers/${serverId}/progress-assets`, { responseType: 'blob' }); const url = URL.createObjectURL(response.data); const a = document.createElement('a'); a.href = url; a.download = `gitea-review-progress-${serverId}.zip`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); ElMessage.success('组件已下载；解压后按安装说明接入已有 Gitea 容器') } catch (e) { showError(e) } }
async function write(method, path, data) { if (!csrf.value) csrf.value = (await api.get('/ai-review/csrf')).data.token; return api.request({ method, url: '/ai-review'+path, data, headers: { 'X-Review-CSRF': csrf.value } }) }
async function loadTasks() { const current = ++taskGeneration; try { const r = await api.get('/ai-review/tasks', { params: { page: page.value, repository: filterRepo.value || undefined } }); if (!disposed && current === taskGeneration) { tasks.value = r.data.items; total.value = r.data.total } } catch (e) { if (!disposed && current === taskGeneration) showError(e) } }
async function refresh() { try { const [c, r, s] = await Promise.all([api.get('/ai-review/config'), api.get('/ai-review/repositories'), api.get('/ai-review/servers')]); config.value = c.data.config; health.value = c.data.health; repositories.value = r.data; servers.value = s.data; await Promise.all([loadTasks(), refreshReviewAttention(true)]) } catch (e) { showError(e) } }
async function saveConfig() { busy.value = true; try { const { api_key_configured, ...payload } = config.value; if (apiKey.value) payload.api_key = apiKey.value; config.value = (await write('put', '/config', payload)).data; apiKey.value = ''; ElMessage.success('设置已保存') } catch (e) { showError(e) } finally { busy.value = false } }
async function testConnection() { testing.value = true; try { const r = await write('post', '/connection-test', {}); connectionMessage.value = r.data.message; connectionOk.value = r.data.ok } catch (e) { showError(e) } finally { testing.value = false } }
function editRepo(row) { repoForm.value = row ? { ...row, bot_token: '' } : { server_id: servers.value[0]?.id, repo_name: '', bot_token: '', enabled: false }; catalog.value = []; repoVisible.value = true; if (!row && repoForm.value.server_id) searchCatalog('') }
async function searchCatalog(q) { const generation = ++catalogGeneration; if (!repoForm.value.server_id) return; catalogLoading.value = true; try { const r = await api.get('/ai-review/catalog', { params: { server_id: repoForm.value.server_id, q } }); if (generation === catalogGeneration) catalog.value = r.data.items } catch (e) { showError(e) } finally { if (generation === catalogGeneration) catalogLoading.value = false } }
async function saveRepo() { busy.value = true; try { const f = repoForm.value; await write(f.id ? 'put' : 'post', '/repositories'+(f.id ? '/'+f.id : ''), { server_id: f.server_id, repo_name: f.repo_name, bot_token: f.bot_token, enabled: f.enabled, revision: f.revision }); repoVisible.value = false; repoForm.value.bot_token = ''; repositories.value = (await api.get('/ai-review/repositories')).data; ElMessage.success('仓库配置已保存') } catch (e) { showError(e) } finally { busy.value = false } }
async function installHook() { busy.value = true; try { await write('post', `/repositories/${hookRepo.value.id}/webhook`, { manager_url: managerURL.value }); hookVisible.value = false; repositories.value = (await api.get('/ai-review/repositories')).data; ElMessage.success('事件通知已安装并验证') } catch (e) { showError(e) } finally { busy.value = false } }
async function rerun(repository, number) { try { await write('post', `/repositories/${repository}/rerun`, { number }); manualVisible.value = false; tab.value = 'tasks'; page.value = 1; await loadTasks(); ElMessage.success('审查任务已排队') } catch (e) { showError(e) } }
async function openTask(id) { try { detail.value = (await api.get('/ai-review/tasks/'+id)).data; detailVisible.value = true } catch (e) { showError(e) } }
async function poll() { if (disposed) return; if (tab.value === 'tasks') await loadTasks(); if (detailVisible.value && detail.value && ['queued', 'running', 'publishing', 'recovering', 'publish_unknown'].includes(detail.value.status)) await openTask(detail.value.id); try { health.value = (await api.get('/ai-review/config')).data.health } catch {} if (!disposed) timer = setTimeout(poll, 5000) }
function onVisible() { if (!document.hidden) loadTasks() }
onMounted(async () => { document.addEventListener('visibilitychange', onVisible); await refresh(); if (!disposed) timer = setTimeout(poll, 5000) })
onUnmounted(() => { disposed = true; taskGeneration++; clearTimeout(timer); document.removeEventListener('visibilitychange', onVisible); apiKey.value = ''; repoForm.value.bot_token = '' })
</script>

<style scoped>
.review-page { min-width: 0; }
.review-page > .section-header .section-title { font-size: 24px; }
.review-page > .section-header p { font-size: 14px; margin-top: 6px; }
.task-record-title { font-size: 20px; margin: 0 16px 0 0; }
.review-toolbar, .review-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; min-width: 0; }
.review-toolbar { justify-content: space-between; margin-bottom: 18px; }
.panel { padding: 24px; }
.muted { color: var(--text-secondary, #64748b); font-size: 13px; line-height: 1.7; }
.review-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 24px; }
.selector { width: 280px; max-width: 100%; }
.full { width: 100%; }
.footer { margin-top: 22px; margin-bottom: 16px; }
.finding { margin-top: 20px; padding: 20px; border: 1px solid #dfe6ed; border-radius: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.finding pre { padding: 12px; overflow: auto; background: #f3f6fa; border-radius: 8px; white-space: pre-wrap; }
.wrap { overflow-wrap: anywhere; }
.failure { color: #b45309; font-size: 12px; }
.el-pagination { margin-top: 20px; flex-wrap: wrap; }
.el-alert { margin-bottom: 16px; }
@media (max-width: 720px) { .review-grid { grid-template-columns: minmax(0, 1fr); } .panel { padding: 14px; } }
</style>
