<template>
  <div class="review-page">
    <template v-if="!detailVisible">
    <div class="section-header review-toolbar">
      <div><h3 class="section-title">AI Code Review</h3><p class="muted">自动分析 PR 变更，提供可追溯的辅助审查意见。</p></div>
      <div class="review-actions"><el-tag :type="config.enabled ? 'success' : 'info'">{{ config.enabled ? '已启用' : '全局关闭' }}</el-tag><el-button :icon="Refresh" @click="refresh">刷新</el-button></div>
    </div>
    <el-alert v-if="error" :title="error" type="error" show-icon closable @close="error = ''" />
    <AIReviewSetup v-if="isPlatformAdmin" :servers="servers" :config="config" :repositories="repositories.filter(row => !row.access_archived)" @changed="refresh" />
    <el-alert title="本地校验后直接发布行内评论；无法可靠定位的问题保留为普通意见。无需 Gitea 补丁。" type="info" show-icon :closable="false" />
    <el-alert v-if="isPlatformAdmin && workerStale" title="Review 工作进程尚未就绪或心跳已过期，自动审查可能暂停。" type="warning" show-icon :closable="false" />
    <el-alert v-if="isPlatformAdmin && progressStale" title="PR 进度同步进程尚未就绪或心跳已过期，审查仍可执行，但 Gitea 页面状态可能延迟。" type="warning" show-icon :closable="false" />
    <el-alert v-if="isPlatformAdmin && feedbackStale" title="人工反馈同步进程尚未就绪或心跳已过期，样本来源超过有效期后将暂停参考；自动审查仍可执行。" type="warning" show-icon :closable="false" />
    <el-tabs v-model="tab">
      <el-tab-pane label="审查任务" name="tasks">
        <div class="glass-card panel">
          <div class="review-toolbar"><el-select v-model="filterRepo" clearable placeholder="全部仓库" aria-label="筛选仓库" class="selector" @change="page = 1; loadTasks()"><el-option v-for="r in repositories" :key="r.id" :value="r.id" :label="`${r.server_name} / ${r.repo_name}${r.access_archived ? '（已归档）' : ''}`" /></el-select><span class="muted">每次任务绑定独立代码版本；无法可靠定位时保留为普通意见。</span></div>
          <el-table :data="tasks" stripe max-height="var(--table-max-height)">
            <el-table-column label="仓库 / PR" min-width="230"><template #default="{ row }">{{ repoName(row.repository) }} #{{ row.number }}</template></el-table-column>
            <el-table-column label="代码版本" min-width="135"><template #default="{ row }"><code>{{ row.progress?.head?.slice(0, 12) || '等待读取' }}</code></template></el-table-column>
            <el-table-column label="状态" min-width="135"><template #default="{ row }"><el-tag :type="statusType(row.status)">{{ row.progress?.label || statusLabel(row.status) }}</el-tag></template></el-table-column>
            <el-table-column label="当前阶段" min-width="230"><template #default="{ row }">{{ row.stage }}<div v-if="row.progress?.units_total != null" class="muted">完成单元 {{ row.progress.units_completed }}/{{ row.progress.units_total }}</div><div v-if="row.progress?.sync_error" class="failure">状态同步：{{ row.progress.sync_error }}</div></template></el-table-column>
            <el-table-column label="创建时间 ↓" min-width="195"><template #default="{ row }">{{ fmt(row.created) }}<div class="muted">更新：{{ fmt(row.updated) }}</div></template></el-table-column>
            <el-table-column label="操作" min-width="250"><template #default="{ row }"><div class="review-actions"><el-button link type="primary" @click="openTask(row.id)">详情</el-button><el-button v-if="!repoArchived(row.repository) && ['queued', 'running'].includes(row.status)" link type="danger" :loading="cancellingId === row.id" :disabled="Boolean(cancellingId)" @click="cancelTask(row)">强制取消</el-button><el-tag v-if="repoArchived(row.repository)" type="info">已归档</el-tag><template v-else-if="row.status === 'publish_unknown'"><el-button link type="warning" @click="reconcile(row)">核对发布</el-button><el-button link type="warning" @click="confirmUnpublished(row)">确认未发布</el-button></template><el-button v-else link :disabled="!config.enabled || row.status === 'cancelling'" @click="rerun(row.repository, row.number)">重新审查</el-button></div></template></el-table-column>
            <template #empty><el-empty description="启用仓库后，创建或更新 PR 即可自动审查。" /></template>
          </el-table>
          <el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next, total" @current-change="loadTasks" />
        </div>
      </el-tab-pane>
      <el-tab-pane label="仓库管理" name="repositories">
        <div class="glass-card panel">
          <div class="review-toolbar"><span>{{ isPlatformAdmin ? '为仓库配置只读机器人，并安装 PR 事件通知。' : '管理当前组织已接入仓库的审查状态。新增接入请联系平台管理员。' }}</span><el-button v-if="isPlatformAdmin" type="primary" :icon="Plus" @click="editRepo()">添加仓库</el-button></div>
          <el-table :data="repositories" stripe max-height="var(--table-max-height)">
            <el-table-column label="Gitea 实例" min-width="180"><template #default="{ row }">{{ row.server_name }}<div class="muted">{{ row.gitea_version || '请刷新仓库配置以核对版本' }}</div></template></el-table-column>
            <el-table-column prop="repo_name" label="仓库" min-width="220" />
            <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="row.access_archived ? 'warning' : row.enabled ? 'success' : 'info'">{{ row.access_archived ? '已归档' : row.enabled ? '启用' : '关闭' }}</el-tag></template></el-table-column>
            <el-table-column prop="bot_name" label="审查机器人" min-width="150" />
            <el-table-column label="事件通知" min-width="230"><template #default="{ row }"><div>{{ row.hook_status }}</div><small class="muted">最近收到：{{ fmt(row.last_event) }}</small><div v-if="row.reconcile_error" class="failure">{{ row.reconcile_error }}</div></template></el-table-column>
            <el-table-column label="操作" min-width="200"><template #default="{ row }"><span v-if="row.access_archived" class="muted">历史仅供核对；新归属请添加仓库重新接入。</span><div v-else class="review-actions"><el-button link type="primary" @click="editRepo(row)">配置</el-button><el-button v-if="isPlatformAdmin" link @click="downloadProgress(row.server_id)">下载 PR 进度组件</el-button><el-button v-if="isPlatformAdmin" link @click="hookRepo = row; hookVisible = true">事件通知</el-button><el-button link :disabled="!row.enabled || !config.enabled" @click="manualRepo = row; manualVisible = true">审查 PR</el-button></div></template></el-table-column>
          </el-table>
        </div>
      </el-tab-pane>
      <el-tab-pane label="审查经验数据集" name="dataset"><AIReviewDataset :repositories="repositories" /></el-tab-pane>
      <el-tab-pane v-if="isPlatformAdmin" label="模型与设置" name="config">
        <div class="glass-card panel">
          <el-alert v-if="configError" :title="configError" type="error" show-icon :closable="false" /><el-form label-position="top" @submit.prevent="saveConfig">
            <div class="review-grid">
              <el-form-item label="全局启用"><el-switch v-model="config.enabled" aria-label="全局启用 AI Review" /></el-form-item>
              <el-form-item label="执行引擎"><el-select v-model="config.engine" aria-label="执行引擎"><el-option label="原生 OpenCode 会话" value="opencode_native" /></el-select></el-form-item>
              <el-form-item label="内部 New API 地址"><el-input v-model="config.api_base_url" placeholder="填写包含 API 前缀的内网地址" autocomplete="off" /></el-form-item>
              <el-form-item label="模型"><el-input v-model="config.model" placeholder="填写内部实际可用的模型标识" autocomplete="off" /></el-form-item>
              <el-form-item :label="config.api_key_configured ? 'API Key（已配置，留空保留）' : 'API Key（尚未配置）'"><el-input v-model="apiKey" type="password" autocomplete="new-password" placeholder="仅在新增或更换密钥时填写" /></el-form-item>
              <el-form-item label="思考控制协议"><el-select v-model="config.thinking_protocol" @change="value => { if (value === 'provider_default') config.thinking_enabled = false }"><el-option label="服务默认（未控制）" value="provider_default" /><el-option label="Qwen / vLLM（含 New API 透传）" value="qwen_vllm" /><el-option label="DeepSeek 官方协议" value="deepseek" /><el-option label="reasoning_effort 兼容协议" value="reasoning_effort" /></el-select><span class="secondary">按实际服务选择；New API 必须支持透传。服务默认不代表已关闭思考。</span></el-form-item>
              <el-form-item label="启用深度思考"><el-switch v-model="config.thinking_enabled" :disabled="config.thinking_protocol === 'provider_default'" /><span class="secondary">默认关闭；仅对新任务生效。参数被接受不代表服务实际遵守。</span></el-form-item>
              <el-form-item label="思考额度（Token）"><el-input-number v-model="config.thinking_token_budget" :min="128" :max="32768" :precision="0" :disabled="!config.thinking_enabled || config.thinking_protocol !== 'qwen_vllm'" /><span class="secondary">仅 Qwen / vLLM 独立思考预算；需要服务支持，其他协议不发送此额度。</span></el-form-item>
              <el-form-item label="连续 Push 合并窗口（秒）"><el-input-number v-model="config.debounce_seconds" :min="0" :max="120" /></el-form-item>
            </div>
            <el-collapse><el-collapse-item title="审查资源预算" name="limits"><div class="review-grid">
              <el-form-item label="整次审查上限（秒）"><el-input-number v-model="config.review_timeout_seconds" :min="60" :max="3600" :precision="0" /><span class="secondary">首次默认 600 秒；旧配置沿用原任务预算。仅对保存后创建的新任务生效。</span></el-form-item>
              <el-form-item label="整次检索操作额度"><el-input-number v-model="config.context_retrieval_budget" :min="1" :max="200" :precision="0" /><span class="secondary">默认 20 次，按 read / grep / glob 操作计数；耗尽后仅请求提交已有结论。</span></el-form-item>
              <el-form-item label="模型上下文窗口（Token）"><el-input-number v-model="config.model_context_tokens" :min="4096" :max="1048576" :precision="0" /><span class="secondary">默认 262,144，请填写模型服务实际开放的窗口大小。</span></el-form-item>
              <el-form-item label="上下文压缩阈值（Token）"><el-input-number v-model="config.compaction_threshold_tokens" :min="4096" :max="1048576" :precision="0" /><span class="secondary">默认 210,000（210K）。达到阈值后由 OpenCode 压缩并在执行记录中显示；阈值加输出预留不能超过模型窗口，输出额度为 0 时至少预留 8,192。读取行数由模型和原生工具决定。</span></el-form-item>
              <el-form-item label="单次模型请求超时（秒）"><el-input-number v-model="config.model_request_timeout_seconds" :min="60" :max="3600" :precision="0" /></el-form-item>
              <el-form-item label="模型输出额度（Token）"><el-input-number v-model="config.model_max_output_tokens" :min="0" :max="1048576" :precision="0" /><span class="secondary">0 表示跟随模型服务；非零额度可能同时包含思考与正文，不能替代独立思考预算。</span></el-form-item>
              <el-form-item label="正文大小上限（MiB）"><el-input-number v-model="config.model_response_max_mib" :min="1" :max="64" :precision="0" /><span class="secondary">统一限制结构化正文，不累计心跳和进度事件。</span></el-form-item>
            </div><p class="muted" data-testid="time-budget-help">初始单次等待上限：{{ effectiveModelTimeout }} 秒，即单次模型请求上限与整次审查剩余期限减去 40 秒收尾时间中的较小值。实际可用时间会随准备耗时和后续调用递减。资源配置仅对新创建的任务生效，已排队和运行中的任务沿用原预算。</p></el-collapse-item></el-collapse>
            <div class="review-actions footer"><el-button type="primary" :loading="busy" native-type="submit">保存设置</el-button><el-button :loading="testing" @click="testConnection">检查已保存的连接</el-button></div>
          </el-form>
          <el-alert v-if="connectionMessage" :title="connectionMessage" :type="connectionOk ? 'success' : 'warning'" :closable="false" />
          <details v-if="connectionDiagnostic" class="connection-diagnostic" data-testid="connection-diagnostic">
            <summary>查看连接测试详情（已脱敏）</summary>
            <p>诊断编号：{{ connectionDiagnostic.diagnostic_id }} · {{ connectionDiagnostic.time_utc }}</p>
            <p>失败阶段：{{ connectionDiagnostic.phase }} · 接口：{{ connectionDiagnostic.request_path }}</p>
            <p>累计耗时：{{ connectionDiagnostic.elapsed_seconds }} 秒 · 连接超时：{{ connectionDiagnostic.connect_timeout_seconds }} 秒 · 读取超时：{{ connectionDiagnostic.read_timeout_seconds }} 秒</p>
            <p>错误码：{{ connectionDiagnostic.error_code }} · HTTP：{{ connectionDiagnostic.http_status ?? '未收到响应' }} · 异常类型：{{ connectionDiagnostic.exception_type || '响应校验失败' }}</p>
            <p>响应大小：{{ connectionDiagnostic.response_bytes }} 字节；响应摘要：</p>
            <pre style="white-space: pre-wrap; overflow-wrap: anywhere">{{ connectionDiagnostic.response_summary }}</pre>
          </details>
        </div>
      </el-tab-pane>
    </el-tabs>

    <el-dialog append-to-body v-model="repoVisible" :title="repoForm.id ? '配置审查仓库' : '添加审查仓库'" width="min(640px, 95vw)" :close-on-click-modal="false">
      <el-alert v-if="repoError" :title="repoError" type="error" show-icon :closable="false" /><el-form label-position="top">
        <el-form-item label="Gitea 实例"><el-select v-model="repoForm.server_id" :disabled="!!repoForm.id" class="full" @change="catalog = []; repoForm.repo_name = ''; searchCatalog('')"><el-option v-for="s in servers" :key="s.id" :value="s.id" :label="s.name" /></el-select></el-form-item>
        <el-form-item label="仓库"><el-select v-model="repoForm.repo_name" :disabled="!!repoForm.id" filterable remote :remote-method="searchCatalog" :loading="catalogLoading" placeholder="搜索所有者或仓库名称" class="full"><el-option v-for="r in catalog" :key="r.id" :value="r.name" :label="r.name" /><el-option v-if="repoForm.id" :label="repoForm.repo_name" :value="repoForm.repo_name" /></el-select></el-form-item>
        <el-form-item v-if="isPlatformAdmin" label="专用机器人 Token（编辑时留空保留）"><el-input v-model="repoForm.bot_token" type="password" autocomplete="new-password" /></el-form-item>
        <div v-if="isPlatformAdmin" class="permission-guide"><strong>账号与 Token 的权限分别配置</strong><dl><dt>机器人账号</dt><dd>独立非管理员；仓库协作者选择 Read／只读。组织和团队也不能授予代码写入或管理权限。</dd><dt>Token 权限</dt><dd><code>read:user</code>、<code>write:repository</code>、<code>write:issue</code>。私有仓库不能选择仅公开资源。</dd></dl><p>Token 的写权限用于发布审查和进度评论，账号自身仍保持代码只读。已有 Token 满足要求时，可以继续使用。</p></div>
        <el-form-item label="启用该仓库"><el-switch v-model="repoForm.enabled" /></el-form-item>
      </el-form>
      <template #footer><el-button @click="repoVisible = false">取消</el-button><el-button type="primary" :loading="busy" @click="saveRepo">保存仓库</el-button></template>
    </el-dialog>
    <el-dialog append-to-body v-model="hookVisible" title="安装或核对 PR 事件通知" width="min(600px, 95vw)">
      <el-alert v-if="hookError" :title="hookError" type="error" show-icon :closable="false" /><p>{{ hookRepo?.server_name }} / {{ hookRepo?.repo_name }}</p><el-form label-position="top"><el-form-item label="Gitea 能够访问的 Manager 地址"><el-input v-model="managerURL" placeholder="填写 Manager 完整访问地址" /></el-form-item></el-form>
      <p class="muted">将使用现有服务器管理凭据安装 Webhook，并回读验证。该地址必须在部署白名单内。</p>
      <template #footer><el-button @click="hookVisible = false">取消</el-button><el-button type="primary" :loading="busy" @click="installHook">安装并验证</el-button></template>
    </el-dialog>
    <el-dialog append-to-body v-model="manualVisible" title="人工触发 PR 审查" width="min(460px, 95vw)"><el-alert v-if="manualError" :title="manualError" type="error" show-icon :closable="false" /><p>{{ manualRepo?.repo_name }}</p><el-form label-position="top"><el-form-item label="PR 编号"><el-input-number v-model="manualNumber" :min="1" /></el-form-item></el-form><template #footer><el-button @click="manualVisible = false">取消</el-button><el-button type="primary" :loading="rerunning" :disabled="rerunning" @click="rerun(manualRepo.id, manualNumber)">开始审查</el-button></template></el-dialog>
    </template>
    <ReviewTaskDetail v-else :open="detailVisible" :task="detail" :loading="detailLoading" :error="detailError" :cancelling="cancellingId === detail?.id" :can-cancel="!repoArchived(detail?.repository)" @cancel="cancelTask(detail)" :repository="repositories.find(r => r.id === detail?.repository)?.repo_name || '仓库'" :instance="repositories.find(r => r.id === detail?.repository)?.server_name || ''" @close="closeDetail" />
  </div>
</template>

<script setup>
import { computed, getCurrentInstance, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { api } from '../api'
import { isPlatformAdmin, scopedServers } from '../composables/accessState.js'
import AIReviewDataset from '../components/AIReviewDataset.vue'
import AIReviewSetup from '../components/AIReviewSetup.vue'
import ReviewTaskDetail from '../components/ReviewTaskDetail.vue'
import { errorMessage, statusType } from '../components/reviewDisplay'

const tab = ref('tasks'), error = ref(''), busy = ref(false), testing = ref(false), csrf = ref('')
const app = getCurrentInstance()
const config = ref({ enabled: false }), apiKey = ref(''), health = ref([]), servers = ref([]), repositories = ref([])
const tasks = ref([]), total = ref(0), page = ref(1), filterRepo = ref(null), detail = ref(null), detailVisible = ref(false)
const repoVisible = ref(false), repoForm = ref({}), catalog = ref([]), catalogLoading = ref(false)
const hookVisible = ref(false), hookRepo = ref(null), managerURL = ref(window.location.origin)
const manualVisible = ref(false), manualRepo = ref(null), manualNumber = ref(1)
const connectionMessage = ref(''), connectionOk = ref(false), connectionDiagnostic = ref(null)
const repoError = ref(''), hookError = ref(''), manualError = ref(''), configError = ref('')
const detailError = ref(''), detailLoading = ref(false), rerunning = ref(false), cancellingId = ref('')
let taskGeneration = 0, detailGeneration = 0, repoGeneration = 0, listScrollTop = 0, detailController
watch(repoVisible, visible => { if (!visible) { repoGeneration++; catalogGeneration++; repoForm.value.bot_token = ''; repoError.value = '' } })
let hookGeneration = 0
watch(hookVisible, () => { hookGeneration++; hookError.value = '' })
watch(manualVisible, () => { manualError.value = '' })
watch(detailVisible, visible => { if (!visible && (detail.value || detailLoading.value)) closeDetail() })
let timer, catalogGeneration = 0, disposed = false, lastHealthPoll = 0
const workerStale = computed(() => config.value.enabled && (!health.value.find(h => h.name === 'worker') || Date.now()/1000-health.value.find(h => h.name === 'worker').updated > 90))
const feedbackStale = computed(() => config.value.enabled && (!health.value.find(h => h.name === 'feedback') || Date.now()/1000-health.value.find(h => h.name === 'feedback').updated > 90))
const progressStale = computed(() => config.value.enabled && (!health.value.find(h => h.name === 'progress') || Date.now()/1000-health.value.find(h => h.name === 'progress').updated > 90))
const fmt = value => value ? new Date(value*1000).toLocaleString('zh-CN') : '尚无'
const repoName = id => { const r = repositories.value.find(r => r.id === id); return r ? `${r.server_name} / ${r.repo_name}${r.access_archived ? '（已归档）' : ''}` : `仓库 ${id ?? ''}` }
const repoArchived = id => Boolean(repositories.value.find(row => row.id === id)?.access_archived)
const statusLabel = s => ({ cancelling: '取消中', cancelled: '已取消', queued: '等待执行', running: '分析中', publishing: '发布与核对', recovering: '核对发布结果', publish_unknown: '发布待核对', succeeded: '已完成', superseded: '版本已过期', failed: '执行失败' }[s] || s)

const effectiveModelTimeout = computed(() => Math.max(0, Math.min(config.value.model_request_timeout_seconds ?? 600, (config.value.review_timeout_seconds ?? 600) - 40)))
const showError = e => { error.value = errorMessage(e) }
async function downloadProgress(serverId) { try { const response = await api.get(`/ai-review/servers/${serverId}/progress-assets`, { responseType: 'blob' }); const url = URL.createObjectURL(response.data); const a = document.createElement('a'); a.href = url; a.download = `gitea-review-progress-${serverId}.zip`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); ElMessage.success('组件已下载；解压后按安装说明接入已有 Gitea 容器') } catch (e) { showError(e) } }
async function write(method, path, data) { if (!csrf.value) csrf.value = (await api.get('/ai-review/csrf')).data.token; return api.request({ method, url: '/ai-review'+path, data, headers: { 'X-Review-CSRF': csrf.value } }) }
async function loadTasks() { const current = ++taskGeneration; try { const r = await api.get('/ai-review/tasks', { params: { page: page.value, repository: filterRepo.value || undefined } }); if (!disposed && current === taskGeneration) { tasks.value = r.data.items; total.value = r.data.total } } catch (e) { if (!disposed && current === taskGeneration) showError(e) } }
async function refresh() { try { const [c, r, s] = await Promise.all([api.get('/ai-review/config'), api.get('/ai-review/repositories'), api.get('/ai-review/servers')]); config.value = c.data?.config || config.value; health.value = Array.isArray(c.data?.health) ? c.data.health : []; repositories.value = Array.isArray(r.data) ? r.data : repositories.value; servers.value = Array.isArray(s.data) ? scopedServers(s.data) : servers.value; await loadTasks() } catch (e) { showError(e) } }
async function saveConfig() { if (busy.value) return; configError.value = ''; busy.value = true; try { const { api_key_configured, ...payload } = config.value; payload.engine = 'opencode_native'; if (apiKey.value) payload.api_key = apiKey.value; config.value = (await write('put', '/config', payload)).data; apiKey.value = ''; ElMessage.success('设置已保存；资源预算与压缩阈值仅对后续新任务生效') } catch (e) { configError.value = errorMessage(e) } finally { busy.value = false } }
async function testConnection() { testing.value = true; connectionMessage.value = ''; connectionDiagnostic.value = null; try { const r = await write('post', '/connection-test', {}); connectionMessage.value = r.data.message; connectionOk.value = r.data.ok } catch (e) { connectionOk.value = false; connectionMessage.value = errorMessage(e); connectionDiagnostic.value = e.response?.data?.diagnostic || null } finally { testing.value = false } }
function editRepo(row) { repoGeneration++; repoError.value = ''; repoForm.value = row ? { ...row, bot_token: '' } : { server_id: servers.value[0]?.id, repo_name: '', bot_token: '', enabled: false }; catalog.value = []; repoVisible.value = true; if (!row && repoForm.value.server_id) searchCatalog('') }
async function searchCatalog(q) { const generation = ++catalogGeneration; if (!repoForm.value.server_id) return; catalogLoading.value = true; try { const r = await api.get('/ai-review/catalog', { params: { server_id: repoForm.value.server_id, q } }); if (!disposed && generation === catalogGeneration) catalog.value = r.data.items } catch (e) { if (!disposed && generation === catalogGeneration) repoError.value = errorMessage(e) } finally { if (generation === catalogGeneration) catalogLoading.value = false } }
async function reloadRepositories() { try { repositories.value = (await api.get('/ai-review/repositories')).data } catch { error.value = '配置已保存，但仓库列表刷新失败；请刷新核对。' } }
async function saveRepo() {
  if (busy.value) return
  busy.value = true; repoError.value = ''; const current = repoGeneration
  try { const f = repoForm.value; await write(f.id ? 'put' : 'post', '/repositories'+(f.id ? '/'+f.id : ''), isPlatformAdmin.value ? { server_id: f.server_id, repo_name: f.repo_name, bot_token: f.bot_token, enabled: f.enabled, revision: f.revision } : { enabled: f.enabled, revision: f.revision });
    if (disposed || current !== repoGeneration) return
    repoVisible.value = false; repoForm.value.bot_token = ''; ElMessage.success('仓库配置已保存'); await reloadRepositories()
  } catch (e) { if (!disposed && current === repoGeneration) repoError.value = errorMessage(e) } finally { busy.value = false }
}
async function installHook() { if (busy.value) return; busy.value = true; hookError.value = ''; const request = hookGeneration; const target = hookRepo.value.id; try { await write('post', `/repositories/${target}/webhook`, { manager_url: managerURL.value }); if (request !== hookGeneration) return; hookVisible.value = false; ElMessage.success('事件通知已安装并验证'); await reloadRepositories() } catch (e) { if (request === hookGeneration) hookError.value = errorMessage(e) } finally { busy.value = false } }
async function rerun(repository, number) { if (rerunning.value) return; rerunning.value = true; manualError.value = ''; try { await write('post', `/repositories/${repository}/rerun`, { number }); manualVisible.value = false; tab.value = 'tasks'; page.value = 1; await loadTasks(); ElMessage.success('审查任务已排队') } catch (e) { if (manualVisible.value) manualError.value = errorMessage(e); else showError(e) } finally { rerunning.value = false } }

async function cancelTask(row) {
  if (!row || cancellingId.value) return
  cancellingId.value = row.id
  try {
    await ElMessageBox.confirm(`取消 ${repoName(row.repository)} #${row.number} 的本次审查？当前版本不会自动重跑，已有日志和诊断将保留。`, '强制取消审查', { confirmButtonText: '强制取消', cancelButtonText: '继续审查', type: 'warning' })
    const response = await write('post', `/tasks/${row.id}/cancel`, {})
    taskGeneration++; detailGeneration++; detailController?.abort(); detailLoading.value = false
    if (detail.value?.id === row.id) detail.value = response.data.task
    tasks.value = tasks.value.map(item => item.id === row.id ? response.data.task : item)
    ElMessage.success(response.data.task.status === 'cancelled' ? '任务已取消' : '已接受取消，正在确认执行停止')
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') {
      if (detailVisible.value) detailError.value = errorMessage(e)
      else showError(e)
      if (e.response?.data?.task) {
        if (detail.value?.id === row.id) detail.value = e.response.data.task
        tasks.value = tasks.value.map(item => item.id === row.id ? e.response.data.task : item)
      }
    }
  } finally { cancellingId.value = '' }
}

async function reconcile(row) { try { await write('post', `/tasks/${row.id}/reconcile`, {}); ElMessage.success('已请求核对，不会盲目重发评论') } catch (e) { showError(e) } }
async function closeDetail() {
  detailGeneration++; detailController?.abort(); detailLoading.value = false; detailError.value = ''; detail.value = null; detailVisible.value = false
  updateTaskQuery('')
  await nextTick(); const main = document.querySelector('.app-main'); if (main) main.scrollTop = listScrollTop
}
function taskQuery() { return String(app?.proxy?.$route?.query?.task || new URLSearchParams(window.location.search).get('task') || '') }
function updateTaskQuery(id) {
  const route = app?.proxy?.$route, router = app?.proxy?.$router
  if (route && router) { const query = { ...route.query }; if (id) query.task = id; else delete query.task; router.replace({ path: route.path, query }); return }
  const url = new URL(window.location.href)
  if (id) url.searchParams.set('task', id); else url.searchParams.delete('task')
  window.history.replaceState(window.history.state, '', url)
}
async function openTask(id, polling = false) {
  const current = ++detailGeneration
  detailController?.abort(); detailController = new AbortController()
  if (!polling) {
    const main = document.querySelector('.app-main'); listScrollTop = main?.scrollTop || 0
    detail.value = null; detailVisible.value = true; detailError.value = ''
    if (taskQuery() !== String(id)) updateTaskQuery(String(id))
  }
  detailLoading.value = true
  try { const r = await api.get('/ai-review/tasks/'+id, { params: { evidence: '0' }, signal: detailController.signal }); if (!disposed && current === detailGeneration && detailVisible.value) detail.value = r.data }
  catch (e) { if (e?.name !== 'CanceledError' && e?.code !== 'ERR_CANCELED' && !disposed && current === detailGeneration) detailError.value = errorMessage(e) }
  finally { if (current === detailGeneration) detailLoading.value = false }
}
async function poll() {
  if (disposed) return
  if (detailVisible.value && detail.value) {
    const active = ['queued', 'running', 'cancelling', 'publishing', 'recovering', 'publish_unknown'].includes(detail.value.status)
    if (active || (detail.value.status !== 'cancelled' && detail.value.log_sync?.complete === false)) await openTask(detail.value.id, true)
  } else if (tab.value === 'tasks') await loadTasks()
  if (isPlatformAdmin.value && Date.now() - lastHealthPoll >= 5000) { lastHealthPoll = Date.now(); try { health.value = (await api.get('/ai-review/config')).data.health } catch {} }
  if (!disposed) timer = setTimeout(poll, detailVisible.value ? (document.hidden ? 5000 : 1000) : 5000)
}

async function confirmUnpublished(row) {
  try {
    await ElMessageBox.confirm('仅当你已确认 Gitea 不再处理原请求、没有正式 Review，并已处理机器人残留草稿时使用。此操作不删除 Gitea 评论；确认后可人工重新审查。', '确认原任务未发布', { confirmButtonText: '已核对，确认未发布', cancelButtonText: '取消', type: 'warning' })
    await write('post', `/tasks/${row.id}/reconcile`, { confirm_unpublished: true })
    await loadTasks()
    ElMessage.success('已记录未发布，可人工重新审查')
  } catch (e) { if (e !== 'cancel' && e !== 'close') showError(e) }
}
onMounted(async () => { await refresh(); if ((app?.proxy?.$route?.path === '/ai-reviews' || window.location.pathname.endsWith('/ai-reviews')) && taskQuery()) await openTask(taskQuery()); if (!disposed) timer = setTimeout(poll, 5000) })
onUnmounted(() => { disposed = true; taskGeneration++; detailGeneration++; detailController?.abort(); repoGeneration++; catalogGeneration++; clearTimeout(timer); apiKey.value = ''; repoForm.value.bot_token = '' })
</script>

<style scoped>
.review-page { min-width: 0; }
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
.permission-guide { padding: 14px; margin-bottom: 18px; border-radius: 10px; background: #f4f7fb; font-size: 13px; line-height: 1.75; }
.permission-guide dl { display: grid; grid-template-columns: 90px minmax(0, 1fr); gap: 8px; }
.permission-guide dd { margin: 0; overflow-wrap: anywhere; }
.failure { color: #b45309; font-size: 12px; }
.el-pagination { margin-top: 20px; flex-wrap: wrap; }
.el-alert { margin-bottom: 16px; }
@media (max-width: 720px) { .review-grid { grid-template-columns: minmax(0, 1fr); } .panel { padding: 14px; } }
</style>
