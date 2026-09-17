<template>
  <div class="policy-center">
    <PolicyTemplateEditor v-if="editorVisible" :item="editorItem" :copy="editorCopy" :copy-across="editorAcross" :selected-organization="organization" :server-id="serverId" :server-name="servers.find(s => s.id === serverId)?.name" :schema="schema" :csrf-token="csrfToken" @close="editorVisible = false" @saved="templateSaved" />
    <template v-else>
    <div class="section-header policy-toolbar">
      <div><h3 class="section-title">仓库策略中心</h3><p class="policy-muted">选择实例和组织 · 创建模板 · 预览后批量下发</p></div>
      <div class="policy-actions"><el-select v-model="serverId" placeholder="选择 Gitea 实例" aria-label="选择 Gitea 实例" class="policy-select" @change="changeServer">
        <el-option v-for="server in servers" :key="server.id" :value="server.id" :label="server.name" />
      </el-select><PolicyOrganizationSelect v-model="organization" :server-id="serverId" class="policy-select" @update:model-value="changeOrganization" /></div>
    </div>
    <el-alert v-if="error" :title="error" type="error" show-icon closable @close="error = ''" />
    <el-tabs v-model="tab">
      <el-tab-pane label="策略模板" name="templates">
        <div class="glass-card policy-panel">
          <div class="policy-toolbar"><div><span>{{ organization ? organization.name + ' 的策略模板' : '尚未归属组织的旧模板' }}</span><el-checkbox v-if="organization" v-model="showLegacy" @change="loadTemplates">查看尚未归属组织的模板</el-checkbox></div><el-button type="primary" :disabled="!organization" :icon="Plus" @click="newTemplate">创建模板</el-button></div>
          <el-table :data="templates" stripe max-height="var(--table-max-height)">
            <el-table-column prop="name" label="模板名称" min-width="180" />
            <el-table-column label="归属组织" min-width="160"><template #default="{ row }">{{ row.organization?.name || '尚未归属组织' }}</template></el-table-column>
            <el-table-column prop="description" label="说明" min-width="200" />
            <el-table-column label="规则数" width="90"><template #default="{ row }">{{ row.rules.length }}</template></el-table-column>
            <el-table-column label="更新时间" min-width="180"><template #default="{ row }">{{ fmt(row.updated_at) }}</template></el-table-column>
            <el-table-column label="操作" min-width="270"><template #default="{ row }"><div class="policy-actions">
              <el-button link type="primary" :disabled="!row.organization" @click="chooseTemplate(row)">选择仓库</el-button>
              <el-button link @click="editTemplate(row)">编辑</el-button>
              <el-button link :disabled="!row.organization" @click="editTemplate(row, true)">另存为</el-button><el-button link :disabled="!row.organization" @click="editTemplate(row, false, true)">复制到其他组织</el-button>
              <el-button link type="danger" @click="deleteTemplate(row)">删除</el-button>
            </div></template></el-table-column>
            <template #empty>选择组织后创建模板，或整理尚未归属组织的旧模板。</template>
          </el-table>
        </div>
      </el-tab-pane>
      <el-tab-pane label="批量应用" name="apply">
        <div class="glass-card policy-panel">
          <div class="policy-toolbar">
            <el-select v-model="templateId" placeholder="选择策略模板" aria-label="选择策略模板" class="policy-select">
              <el-option v-for="item in applicableTemplates" :key="item.id" :value="item.id" :label="item.name" />
            </el-select>
            <el-input v-model="repoQuery" placeholder="搜索仓库名称" aria-label="搜索仓库名称" clearable class="policy-select" @keyup.enter="searchRepos" @clear="searchRepos" />
            <el-button :icon="Search" :disabled="!organization" @click="searchRepos">搜索</el-button>
            <el-button :loading="selectingAll" :disabled="!organization || selectingAll" @click="selectAllResults">选择全部当前结果</el-button>
          </div>
          <el-alert title="仅新增或更新模板指定的规则，保留其他规则。分支名称原样使用；团队不会跨组织按同名替换。" type="info" :closable="false" />
          <el-table ref="repoTable" v-loading="loadingRepos" :data="repos" row-key="id" max-height="min(500px, var(--table-max-height))" stripe @select="selectRow" @select-all="selectPage">
            <el-table-column type="selection" width="48" :selectable="row => row.manageable" />
            <el-table-column prop="full_name" label="仓库" min-width="220" />
            <el-table-column prop="default_branch" label="默认分支" min-width="130" />
            <el-table-column label="可用性" min-width="200"><template #default="{ row }">
              <span v-if="row.manageable">可管理</span><span v-else class="policy-muted">{{ row.archived ? '仓库已归档' : row.mirror ? '拉取镜像不支持' : 'Token 无管理权限' }}</span>
            </template></el-table-column>
          </el-table>
          <div class="policy-toolbar policy-footer">
            <el-pagination v-model:current-page="repoPage" :page-size="50" :total="repoTotal" layout="prev, pager, next, total" @current-change="loadRepos" />
            <div class="policy-actions"><span>已选 {{ selectedIds.size }} 个仓库</span><el-button :disabled="!selectedIds.size" @click="clearSelection">清空</el-button>
              <el-button type="primary" :loading="busy" :disabled="!templateId || !selectedIds.size || selectingAll" @click="preview">预览差异</el-button></div>
          </div>
        </div>
      </el-tab-pane>
      <el-tab-pane label="执行记录" name="history">
        <div class="glass-card policy-panel">
          <div class="policy-toolbar"><span>记录包含预览、共享管理员身份、模板快照与执行结果。</span><el-button :icon="Refresh" @click="loadHistory">刷新</el-button></div>
          <el-table :data="history" stripe @row-click="openBatch" max-height="var(--table-max-height)">
            <el-table-column prop="id" label="任务" width="80" />
            <el-table-column prop="template_name" label="模板" min-width="180" />
            <el-table-column prop="actor" label="操作人" min-width="130" />
            <el-table-column prop="total" label="仓库数" width="90" />
            <el-table-column label="状态" min-width="150"><template #default="{ row }"><el-tag :type="statusType(row.status)">{{ statusLabel(row.status) }}</el-tag></template></el-table-column>
            <el-table-column label="创建时间" min-width="180"><template #default="{ row }">{{ fmt(row.created_at) }}</template></el-table-column>
            <el-table-column label="操作" width="100"><template #default="{ row }"><el-button link type="primary" @click.stop="openBatch(row)">查看详情</el-button></template></el-table-column>
          </el-table>
          <el-pagination v-model:current-page="historyPage" :page-size="20" :total="historyTotal" layout="prev, pager, next, total" @current-change="loadHistory" />
        </div>
      </el-tab-pane>
    </el-tabs>
    </template>

    <el-dialog v-model="batchVisible" :title="batch ? `任务 #${batch.id} · ${batch.template_name}` : '任务详情'" width="min(1200px, 97vw)" top="4vh" :close-on-click-modal="false" @closed="stopPolling">
      <template v-if="batch">
        <div class="policy-toolbar"><div class="policy-actions"><el-tag :type="statusType(batch.status)">{{ statusLabel(batch.status) }}</el-tag><span>{{ batch.actor }} · {{ batch.total }} 个仓库</span></div>
          <el-button @click="showSnapshot = !showSnapshot">{{ showSnapshot ? '收起模板快照' : '查看当时的模板' }}</el-button></div>
        <el-progress :percentage="batch.progress_percent" :status="batch.status === 'completed' ? 'success' : undefined" />
        <el-alert v-if="batch.error" :title="batch.error" type="error" :closable="false" />
        <pre v-if="showSnapshot" class="policy-json">{{ readableSnapshot }}</pre>
        <div class="policy-toolbar policy-counts"><span v-for="(count, status) in batch.counts" :key="status">{{ statusLabel(status) }} {{ count }}</span></div>
        <div class="policy-toolbar"><el-select v-model="resultStatus" clearable placeholder="筛选仓库结果" aria-label="筛选仓库结果" class="policy-select" @change="filterResults">
          <el-option v-for="(_, status) in batch.counts" :key="status" :value="status" :label="statusLabel(status)" />
        </el-select><el-button :icon="Refresh" @click="refreshBatch">刷新进度</el-button></div>
        <el-table :data="results" row-key="id" max-height="min(320px, var(--table-max-height))" stripe>
          <el-table-column type="expand"><template #default="{ row }"><div class="policy-diff">
            <p>保留的已有规则：{{ row.retained_rules?.join('、') || '无' }}</p>
            <p v-if="row.repository_url"><el-link :href="row.repository_url + '/settings/branches'" target="_blank" rel="noopener noreferrer" type="primary">在 Gitea 查看此仓库的原生分支设置与状态检查记录</el-link></p>
            <p v-for="match in row.matches" :key="match.rule_name"><strong>{{ match.rule_name }}</strong> · 优先级 {{ match.priority }} · 当前匹配：{{ match.branches.join('、') || '暂无，规则仍会约束未来匹配分支' }}</p>
            <div v-for="change in row.diff" :key="change.rule_name"><h4>{{ change.action === 'create' ? '新增' : '修改' }} {{ change.rule_name }}</h4>
              <el-table :data="change.fields" size="small" max-height="var(--table-detail-max-height)"><el-table-column label="规则项" min-width="200"><template #default="{ row: field }">{{ labels[field.field] || field.field }}</template></el-table-column>
                <el-table-column label="修改前" min-width="170"><template #default="{ row: field }">{{ display(field.before) }}</template></el-table-column>
                <el-table-column label="修改后" min-width="170"><template #default="{ row: field }">{{ display(field.after) }}</template></el-table-column></el-table>
            </div>
            <p v-if="!row.diff.length">{{ row.status === 'blocked' ? '该仓库预检未通过，没有执行修改。' : '没有需要修改的规则。' }}</p>
            <details v-if="row.actual"><summary>查看执行前、预期和实际回读内容</summary><pre class="policy-json">{{ JSON.stringify({ '执行前': row.before, '预期': row.desired, '实际回读': row.actual, '逐条操作': row.operations }, null, 2) }}</pre></details>
          </div></template></el-table-column>
          <el-table-column label="仓库" min-width="200"><template #default="{ row }">{{ row.repo_name || `仓库 ID ${row.repo_id}` }}</template></el-table-column>
          <el-table-column label="状态" width="140"><template #default="{ row }"><el-tag :type="statusType(row.status)">{{ statusLabel(row.status) }}</el-tag></template></el-table-column>
          <el-table-column label="变更规则数" width="120"><template #default="{ row }">{{ row.diff.length }}</template></el-table-column>
          <el-table-column prop="error" label="说明 / 失败原因" min-width="330" />
        </el-table>
        <el-pagination v-model:current-page="resultPage" :page-size="50" :total="resultTotal" layout="prev, pager, next, total" @current-change="refreshBatch" />
        <el-alert v-if="batch.status === 'ready' && batch.counts.blocked" :title="`${batch.counts.blocked} 个仓库被阻塞，只会执行其余通过预检的仓库。`" type="warning" :closable="false" />
      </template>
      <template #footer><el-button @click="batchVisible = false">关闭</el-button>
        <el-button v-if="batch?.status === 'ready'" type="primary" :loading="busy" :disabled="!applicableCount" @click="apply">应用 {{ applicableCount }} 个可执行仓库</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh, Search } from '@element-plus/icons-vue'
import { api } from '../api'
import PolicyTemplateEditor from '../components/PolicyTemplateEditor.vue'
import PolicyOrganizationSelect from '../components/PolicyOrganizationSelect.vue'

const prefix = '/repository-policies'
const servers = ref([]), serverId = ref(null), templates = ref([]), templateId = ref(null), tab = ref('templates')
const error = ref(''), busy = ref(false), schema = ref({}), organization = ref(null), showLegacy = ref(false)
const applicableTemplates = computed(() => templates.value.filter(x => x.organization?.id === organization.value?.id))
const repos = ref([]), repoQuery = ref(''), repoPage = ref(1), repoTotal = ref(0), repoTable = ref(null), selectedIds = ref(new Set())
const loadingRepos = ref(false), selectingAll = ref(false), history = ref([]), historyPage = ref(1), historyTotal = ref(0)
const editorVisible = ref(false), editorItem = ref(null), editorCopy = ref(false), editorAcross = ref(false)
const batchVisible = ref(false), batch = ref(null), results = ref([]), resultPage = ref(1), resultTotal = ref(0), resultStatus = ref('')
const showSnapshot = ref(false), snapshot = ref([])
let csrfToken = '', timer = null, disposed = false, repoRequest = 0, templateRequest = 0, selectionRequest = 0, detailRequest = 0
const labels = {
  rule_name: '分支规则', priority: '优先级', required_approvals: '必须审批人数', team_org: '团队所属组织',
  enable_push: '允许直接 Push', enable_push_whitelist: '直接 Push 限定允许名单', push_whitelist_deploy_keys: '允许有写权限的部署密钥 Push',
  enable_force_push: '允许 Force Push', enable_force_push_allowlist: 'Force Push 限定允许名单', force_push_allowlist_deploy_keys: '允许部署密钥 Force Push',
  enable_merge_whitelist: '合并限定允许名单', enable_status_check: '要求 CI / 状态检查通过', enable_approvals_whitelist: '审批限定指定人员或团队',
  block_on_rejected_reviews: '有拒绝意见时阻止合并', block_on_official_review_requests: '有待处理审查请求时阻止合并', block_on_outdated_branch: '分支落后时阻止合并',
  dismiss_stale_approvals: '新提交使旧审批失效', ignore_stale_approvals: '旧提交的审批不计数', require_signed_commits: '必须使用签名提交', block_admin_merge_override: '管理员也必须遵守合并规则',
  push_whitelist_usernames: '允许直接 Push 的人员', push_whitelist_teams: '允许直接 Push 的团队', force_push_allowlist_usernames: '允许 Force Push 的人员', force_push_allowlist_teams: '允许 Force Push 的团队',
  merge_whitelist_usernames: '允许合并的人员', merge_whitelist_teams: '允许合并的团队', approvals_whitelist_username: '允许审批的人员', approvals_whitelist_teams: '允许审批的团队', status_check_contexts: '必须通过的 CI 检查名称 / 匹配模式',
  protected_file_patterns: '禁止修改的文件匹配模式', unprotected_file_patterns: '允许直接 Push 的文件例外',
}
const statuses = { preview_queued: '等待预览', previewing: '正在预览', ready: '预览完成', apply_queued: '等待执行', applying: '正在执行', completed: '执行成功', completed_with_errors: '部分仓库未成功', interrupted: '任务中断', pending: '等待处理', running: '正在处理', changes: '需要修改', unchanged: '无变化', blocked: '已阻塞', success: '已核验成功', failed: '失败', partial: '部分已修改', uncertain: '结果待确认' }
const statusLabel = status => statuses[status] || status
const statusType = status => ['completed', 'success', 'unchanged'].includes(status) ? 'success' : ['failed', 'partial', 'uncertain', 'interrupted', 'completed_with_errors', 'blocked'].includes(status) ? 'danger' : ['ready', 'changes'].includes(status) ? 'warning' : 'info'
const fmt = value => value ? new Date(value).toLocaleString() : '—'
const display = value => value === null ? '不存在' : typeof value === 'boolean' ? (value ? '是' : '否') : Array.isArray(value) ? value.join('、') || '空' : String(value) || '空'
const applicableCount = computed(() => (batch.value?.counts.changes || 0) + (batch.value?.counts.unchanged || 0))
const readableSnapshot = computed(() => JSON.stringify(snapshot.value.map(rule => Object.fromEntries(Object.entries(rule).map(([key, value]) => [labels[key] || key, value]))), null, 2))
function fail(e) { error.value = e.response?.data?.error || e.message || '操作失败'; ElMessage.error(error.value) }
async function send(method, path, data) { return (await api.request({ method, url: prefix + path, data, headers: { 'X-Policy-CSRF': csrfToken } })).data }
async function loadTemplates() {
  const sequence = ++templateRequest, sid = serverId.value, org = organization.value
  const { data } = await api.get(prefix + '/templates', { params: { server_id: sid, org_id: org && !showLegacy.value ? org.id : undefined, legacy: !org || showLegacy.value ? 1 : undefined } })
  if (sequence === templateRequest && sid === serverId.value && !disposed) templates.value = data
}
async function loadRepos() {
  if (!serverId.value || !organization.value) return
  const sequence = ++repoRequest, sid = serverId.value
  loadingRepos.value = true
  try {
    const { data } = await api.get(prefix + '/repositories', { params: { server_id: sid, org: organization.value.name, org_id: organization.value.id, page: repoPage.value, q: repoQuery.value } })
    if (sequence !== repoRequest || sid !== serverId.value || disposed) return
    repos.value = data.items; repoTotal.value = data.total
    await nextTick(); repos.value.forEach(row => repoTable.value?.toggleRowSelection(row, selectedIds.value.has(row.id)))
  } catch (e) { fail(e) } finally { if (sequence === repoRequest) loadingRepos.value = false }
}
async function loadHistory() {
  const sid = serverId.value
  try { const { data } = await api.get(prefix + '/batches', { params: { server_id: sid, page: historyPage.value } }); if (sid === serverId.value) { history.value = data.items; historyTotal.value = data.total } } catch (e) { fail(e) }
}
async function changeServer() { organization.value = null; showLegacy.value = false; historyPage.value = 1; await changeOrganization(); await loadHistory() }
async function changeOrganization() { ++repoRequest; ++selectionRequest; clearSelection(); repos.value = []; repoTotal.value = 0; templates.value = []; templateId.value = null; repoPage.value = 1; showLegacy.value = false; try { await Promise.all([loadTemplates(), loadRepos()]) } catch (e) { fail(e) } }
function searchRepos() { ++selectionRequest; repoPage.value = 1; loadRepos() }
function clearSelection() { selectedIds.value = new Set(); repoTable.value?.clearSelection() }
function selectRow(selection, row) { const copy = new Set(selectedIds.value); if (selection.some(r => r.id === row.id)) copy.add(row.id); else copy.delete(row.id); selectedIds.value = copy }
function selectPage(selection) { const copy = new Set(selectedIds.value); repos.value.forEach(row => { if (selection.some(r => r.id === row.id)) copy.add(row.id); else copy.delete(row.id) }); selectedIds.value = copy }
async function selectAllResults() {
  const sid = serverId.value, query = repoQuery.value, org = organization.value, sequence = ++selectionRequest
  selectingAll.value = true
  try {
    for (let page = 1; ; page++) {
      const { data } = await api.get(prefix + '/repositories', { params: { server_id: sid, org: org.name, org_id: org.id, page, limit: 100, q: query } })
      if (sequence !== selectionRequest || sid !== serverId.value || org.id !== organization.value?.id || disposed) return
      const selected = new Set(selectedIds.value); data.items.filter(r => r.manageable).forEach(r => selected.add(r.id))
      if (selected.size > 5000) throw new Error('本期每批最多选择 5000 个仓库，请缩小搜索范围')
      selectedIds.value = selected
      if (data.items.length < 100) break
    }
    await nextTick(); repos.value.forEach(row => repoTable.value?.toggleRowSelection(row, selectedIds.value.has(row.id)))
  } catch (e) { fail(e) } finally { selectingAll.value = false }
}
function newTemplate() { editorItem.value = null; editorCopy.value = false; editorAcross.value = false; editorVisible.value = true }
function editTemplate(item, copy = false, across = false) { editorItem.value = item; editorCopy.value = copy; editorAcross.value = across; editorVisible.value = true }
async function templateSaved(item) { editorVisible.value = false; organization.value = item.organization; await changeOrganization(); templateId.value = item.id }
async function chooseTemplate(item) { if (!item.organization) return; if (organization.value?.id !== item.organization.id || showLegacy.value) { organization.value = item.organization; await changeOrganization() } templateId.value = item.id; tab.value = 'apply' }
async function deleteTemplate(item) {
  try { await ElMessageBox.confirm('删除模板不会改变仓库规则，历史执行记录仍保留。', '删除 ' + item.name, { type: 'warning', confirmButtonText: '删除模板', cancelButtonText: '取消' }); await send('delete', '/templates/' + item.id); if (templateId.value === item.id) templateId.value = null; await loadTemplates() } catch (e) { if (e !== 'cancel' && e !== 'close') fail(e) }
}
async function preview() { busy.value = true; error.value = ''; try { const template = templates.value.find(t => t.id === templateId.value); const data = await send('post', '/batches', { template_id: template.id, content_hash: template.content_hash, repo_ids: [...selectedIds.value] }); await openBatch(data); loadHistory() } catch (e) { fail(e) } finally { busy.value = false } }
function stopPolling() { if (timer) clearTimeout(timer); timer = null; ++detailRequest }
async function openBatch(row) { stopPolling(); batch.value = row; results.value = []; resultPage.value = 1; resultStatus.value = ''; showSnapshot.value = false; batchVisible.value = true; await refreshBatch() }
async function refreshBatch() {
  if (!batch.value || !batchVisible.value || disposed) return
  if (timer) clearTimeout(timer)
  const id = batch.value.id, sequence = ++detailRequest
  try {
    const { data } = await api.get(prefix + `/batches/${id}`, { params: { page: resultPage.value, status: resultStatus.value || undefined } })
    if (sequence !== detailRequest || batch.value?.id !== id || disposed) return
    batch.value = data.batch; results.value = data.items; resultTotal.value = data.total; snapshot.value = data.template_snapshot
    if (['preview_queued', 'previewing', 'apply_queued', 'applying'].includes(data.batch.status)) timer = setTimeout(refreshBatch, 2000)
    else loadHistory()
  } catch (e) { fail(e) }
}
function filterResults() { resultPage.value = 1; refreshBatch() }
async function apply() { busy.value = true; try { batch.value = await send('post', `/batches/${batch.value.id}/apply`, {}); await refreshBatch() } catch (e) { fail(e) } finally { busy.value = false } }
onMounted(async () => { try { const [s, token, schemaResult] = await Promise.all([api.get(prefix + '/servers'), api.get(prefix + '/csrf'), api.get(prefix + '/schema')]); if (disposed) return; servers.value = s.data; csrfToken = token.data.token; schema.value = schemaResult.data; if (servers.value.length) { serverId.value = servers.value[0].id; await changeServer() } } catch (e) { fail(e) } })
onUnmounted(() => { disposed = true; stopPolling(); ++repoRequest; ++templateRequest; ++selectionRequest })
</script>

<style scoped>
.policy-center { min-width: 0; }
.policy-panel { padding: 22px; min-width: 0; }
.policy-toolbar, .policy-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; min-width: 0; }
.policy-toolbar { justify-content: space-between; margin-bottom: 18px; }
.policy-actions :deep(.el-button + .el-button) { margin-left: 0; }
.policy-actions :deep(.el-button.is-link) { background: transparent; border: 0; box-shadow: none; padding: 4px; }
.policy-select { width: 280px; max-width: 100%; }
.policy-muted { color: #687586; font-size: 13px; line-height: 1.6; }
.policy-footer { margin-top: 18px; margin-bottom: 0; }
.policy-form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 24px; }
.policy-switches { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 14px 0; }
.policy-switches :deep(.el-checkbox) { height: auto; min-height: 30px; margin: 0; white-space: normal; }
.policy-switches :deep(.el-checkbox__label) { white-space: normal; line-height: 1.5; }
.policy-import-list { display: flex; flex-direction: column; gap: 10px; margin: 20px 0; }
.policy-diff { padding: 12px 28px; }
.policy-json { max-height: 450px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; background: #f4f6fa; padding: 16px; border-radius: 8px; line-height: 1.6; }
.policy-counts { justify-content: flex-start; margin-top: 18px; }
.policy-center :deep(.el-pagination) { flex-wrap: wrap; row-gap: 8px; margin-top: 12px; }
.policy-center :deep(.el-alert) { margin-bottom: 16px; }
@media (max-width: 760px) { .policy-form-grid, .policy-switches { grid-template-columns: minmax(0, 1fr); } .policy-panel { padding: 12px; } .policy-diff { padding: 10px; } }
</style>
