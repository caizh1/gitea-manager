<template>
  <div class="approval-page">
    <div class="approval-heading"><div><h2>审批规则</h2><p>复用 Gitea 原生审批，按总人数和必需审批组逐项核对。</p></div><el-button type="primary" @click="openCreate">新建规则</el-button></div>
    <el-alert title="开发验收阶段 · 正式强制接入尚未发布" type="warning" :closable="false" show-icon>
      APR-RACE-001（P2）已记录并暂缓修复。当前可配置规则、预检和导出影子组件；完整接入、长测及现场验收尚未完成。
    </el-alert>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <el-card v-if="nativeInstances.length" class="approval-card" shadow="never">
      <template #header><strong>原生治理入口</strong></template>
      <div v-for="item in nativeInstances" :key="item.server_id" class="approval-actions">
        <span>{{ item.server_name }}</span>
        <template v-if="item.status === 'native'"><a :href="item.groups_url" target="_blank" rel="noopener">管理群组与层级</a><a :href="item.audit_url" target="_blank" rel="noopener">查看原生审计</a></template>
        <span v-else class="approval-muted">{{ item.status === 'legacy' ? '旧版实例' : item.status === 'not_checked' ? '本次未探测' : '暂时不可用' }}</span><el-button v-if="item.status === 'not_checked' || item.status === 'unavailable'" link :disabled="busy" @click="probeNative(item)">单独探测</el-button>
      </div>
    </el-card>
    <el-card class="approval-card" shadow="never">
      <el-table :data="policies" v-loading="busy" empty-text="尚未配置审批规则">
        <el-table-column label="仓库与规则" min-width="210"><template #default="{ row }"><strong>{{ row.repo_name }}</strong><div>{{ row.rule.name }}</div></template></el-table-column>
        <el-table-column label="适用分支" min-width="140"><template #default="{ row }">{{ row.rule.branches.join('、') }}</template></el-table-column>
        <el-table-column label="审批要求" min-width="150"><template #default="{ row }">至少 {{ row.rule.total }} 人 · {{ row.rule.groups.length }} 个必需组</template></el-table-column>
        <el-table-column label="状态" min-width="160"><template #default="{ row }"><el-tag :type="isNative(row) ? (row.mode === 'native' ? 'success' : 'warning') : 'info'">{{ modes[row.mode] || row.mode }}</el-tag><div class="approval-muted">规则版本 {{ row.revision }}</div></template></el-table-column>
        <el-table-column label="操作" min-width="320"><template #default="{ row }"><div class="approval-actions">
          <a v-if="isNative(row) && row.preview.native_url" :href="row.preview.native_url" target="_blank" rel="noopener">原生管理与状态</a>
          <span v-if="row.mode === 'native_pending_uninstall'" class="approval-muted">原生强制已生效；请导出专用包按稳定项目 ID 卸载旧门禁</span>
          <el-button v-if="row.mode === 'native_pending_uninstall'" link type="warning" :disabled="busy" @click="downloadNativeUninstall(row)">导出旧Hook卸载包</el-button>
          <el-button v-if="!isNative(row)" link type="primary" :disabled="busy || !['draft', 'disabled'].includes(row.mode)" @click="edit(row)">编辑</el-button>
          <el-button v-if="!isNative(row)" link type="primary" :disabled="busy" @click="preview(row)">接入预检</el-button>
          <el-button v-if="!isNative(row)" link type="primary" :disabled="busy || !['draft', 'disabled', 'shadow'].includes(row.mode)" @click="openBundle(row)">安装组件</el-button>
          <el-button v-if="!isNative(row)" link :disabled="busy" @click="openStatus(row)">检查 PR</el-button>
          <el-button link :disabled="busy" @click="history(row)">执行记录</el-button>
          <el-button v-if="!isNative(row)" link :disabled="busy" @click="nativePreview(row)">导入原生规则</el-button>
          <el-button v-if="!isNative(row)" link type="danger" :disabled="busy || row.mode === 'disabled'" @click="disable(row)">停用门禁</el-button>
        </div></template></el-table-column>
      </el-table>
    </el-card>
    <el-dialog v-model="editing" :title="form.id ? '编辑审批规则' : '新建审批规则'" width="min(720px, 94vw)" :close-on-click-modal="false">
      <el-form label-position="top" @submit.prevent="save">
        <div class="approval-form-grid">
          <el-form-item label="Gitea 实例"><el-select v-model="form.server_id" :disabled="!!form.id" placeholder="选择实例" @change="changeServer"><el-option v-for="s in servers" :key="s.id" :label="s.name" :value="s.id" /></el-select></el-form-item>
          <el-form-item label="所属组织"><el-select v-model="orgId" :disabled="!!form.id || !form.server_id" placeholder="选择组织" filterable @change="changeOrg"><el-option v-for="o in organizations" :key="o.id" :label="o.name" :value="o.id" /></el-select></el-form-item>
        </div>
        <el-form-item label="仓库"><el-select v-model="form.repo_id" :disabled="!!form.id || !orgId" placeholder="选择已有仓库" filterable remote :remote-method="searchRepos" @change="changeRepo"><el-option v-for="r in repositories" :key="r.id" :label="r.full_name" :value="r.id" :disabled="!r.manageable" /></el-select></el-form-item>
        <el-form-item label="规则名称"><el-input v-model="form.rule.name" maxlength="100" /></el-form-item>
        <el-form-item label="适用分支（明确的分支名称）"><el-select v-model="form.rule.branches" multiple filterable allow-create default-first-option placeholder="输入分支名称并按回车"><el-option v-for="b in form.rule.branches" :key="b" :label="b" :value="b" /></el-select></el-form-item>
        <el-form-item label="不同合格用户总票数"><el-input-number v-model="form.rule.total" :min="1" :max="100" /></el-form-item>
        <el-form-item label="必需审批组"><div class="approval-groups"><div v-for="(g, i) in form.rule.groups" :key="i" class="approval-group">
          <el-select v-model="g.team_id" filterable remote :remote-method="searchTeams" placeholder="选择团队"><el-option v-for="t in teams" :key="t.id" :label="t.name" :value="t.id" /></el-select>
          <el-input-number v-model="g.required" :min="1" :max="100" aria-label="该组要求票数" />
          <el-button :disabled="form.rule.groups.length <= 1" @click="form.rule.groups.splice(i, 1)">移除</el-button>
        </div><el-button :disabled="form.rule.groups.length >= 20" @click="form.rule.groups.push({ team_id: null, required: 1 })">添加审批组</el-button></div></el-form-item>
        <el-alert title="跨组人员可满足多个组，但总票数只计一次。作者不计票；新提交需要重新批准。原生审批池和规则不会被修改。" type="info" :closable="false" />
      </el-form>
      <template #footer><el-button @click="editing = false">取消</el-button><el-button type="primary" :loading="busy" @click="save">保存规则</el-button></template>
    </el-dialog>
    <el-dialog v-model="previewing" title="接入预检与变更预览" width="min(800px, 94vw)">
      <template v-if="previewData"><el-alert title="规则读取预检完成；不代表安装与生产接入验收通过" type="info" :closable="false" />
        <p>存量 PR：{{ previewData.snapshot.length }}；以下规则与分支保护保持原样。</p>
        <el-table :data="previewData.native_rules"><el-table-column prop="rule_name" label="原生规则" /><el-table-column prop="required_approvals" label="原生票数" /><el-table-column label="直接推送"><template #default="{ row }">{{ row.enable_push ? '允许' : '禁止' }}</template></el-table-column></el-table>
        <p v-for="c in previewData.changes" :key="c">拟新增：{{ c }}</p>
        <p v-for="c in previewData.pending_checks" :key="c" class="approval-muted">待验证：{{ c }}</p>
      </template>
    </el-dialog>
    <el-dialog v-model="bundling" title="安装影子组件" width="min(650px, 94vw)" :close-on-click-modal="false">
      <el-alert title="先预检，再导出。导出包不代表已经安装。" type="info" :closable="false" />
      <p>输入 Gitea 所在主机可访问的门禁服务地址，包含 /api/approval-internal。生产影子接入需要 HTTPS。</p>
      <el-input v-model="gateUrl" placeholder="https://manager.example/api/approval-internal" aria-label="门禁服务地址" />
      <p>下载包包含服务凭据，请存放在非公开目录。安装前执行 check，再按包内说明安装；同源代理路径需单独预检。</p>
      <template #footer><el-button @click="bundling = false">取消</el-button><el-button type="primary" :loading="busy" @click="downloadBundle">导出影子安装包</el-button></template>
    </el-dialog>
    <el-dialog v-model="checking" title="检查 PR 的新增审批要求" width="min(760px, 94vw)">
      <div class="approval-actions"><el-input-number v-model="prNumber" :min="1" aria-label="PR 序号" /><el-button :loading="busy" @click="checkPR">读取当前审批</el-button></div>
      <p v-if="checkedExpired">核对结果已过期，请重新读取当前审批。</p>
      <template v-if="prStatus"><p>{{ prStatus.grandfathered ? '存量 PR，保持原生流程' : prStatus.satisfied ? '最近核对：新增要求已满足' : '新增要求尚未满足' }}</p>
        <p>核对时间：{{ new Date(prStatus.checked_at * 1000).toLocaleString() }}</p><p class="approval-audit">当前代码 {{ prStatus.head }} · 原生规则 {{ prStatus.native_rule }}，要求 {{ prStatus.native_required }} 票</p>
        <p v-if="prStatus.total">总票数：{{ prStatus.total.count }} / {{ prStatus.total.required }}</p>
        <el-table :data="prStatus.groups || []"><el-table-column prop="name" label="必需组" /><el-table-column prop="count" label="有效批准" /><el-table-column prop="required" label="要求" /></el-table>
        <p v-for="m in prStatus.missing" :key="m">{{ m }}</p><p class="approval-muted">{{ prStatus.notice }}。这不是最终合并授权。</p>
      </template>
    </el-dialog>
    <el-dialog v-model="showHistory" title="审批执行记录" width="min(850px, 94vw)"><el-table :data="auditRows"><el-table-column prop="created_at" label="时间" min-width="190" /><el-table-column prop="actor" label="操作者" width="120" /><el-table-column label="操作" min-width="170"><template #default="{ row }">{{ actionNames[row.action] || row.action }}</template></el-table-column><el-table-column label="详情" min-width="260"><template #default="{ row }"><span class="approval-audit">{{ JSON.stringify(row.detail) }}</span></template></el-table-column></el-table></el-dialog>
    <el-dialog v-model="showNative" title="导入原生审批规则" width="min(760px, 94vw)"><p>目标：{{ nativePlan?.full_path }}（稳定仓库 ID {{ nativePlan?.repository_id }}）</p><el-table :data="nativePlan?.rules || []"><el-table-column prop="name" label="规则"/><el-table-column prop="required" label="票数" width="90"/><el-table-column label="团队"><template #default="{row}">{{ row.team_ids.join('、') || '全部合格人员' }}</template></el-table-column></el-table><div class="approval-actions"><el-button type="primary" @click="applyNative">确认幂等导入</el-button><a v-if="nativePlan?.native_url" :href="nativePlan.native_url" target="_blank" rel="noopener">打开原生审批页</a></div></el-dialog>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
const policies = ref([]), servers = ref([]), organizations = ref([]), repositories = ref([]), teams = ref([]), nativeInstances = ref([])
const busy = ref(false), error = ref(''), editing = ref(false), previewing = ref(false), previewData = ref(null), orgId = ref(null)
const bundling = ref(false), selected = ref(null), gateUrl = ref(''), checking = ref(false), prNumber = ref(1), prStatus = ref(null)
const checkedExpired = ref(false)
const showHistory = ref(false), auditRows = ref([]), form = ref({})
const showNative = ref(false), nativePlan = ref(null), nativePolicy = ref(null)
let csrf = '', generation = 0, repoRequest = 0, teamRequest = 0, statusRequest = 0, statusExpiry
const modes = { draft: '草稿', shadow: '影子观察', prepared_shadow: '待安装验证', prepared_isolated_enforced: '待隔离安装', isolated_enforced: '隔离强制测试', disabled: '已停用', disable_pending: '待卸载门禁', native_pending_uninstall: '原生已生效，待卸载旧门禁', native: '原生已生效' }
const isNative = row => ['native', 'native_pending_uninstall'].includes(row.mode)
const actionNames = { 'policy.create': '创建规则', 'policy.update': '更新规则', 'preview.complete': '完成规则预检', 'bundle.prepare': '生成安装包', 'installation.reported': '收到安装回执', 'policy.disable': '申请停用门禁', 'installation.removed': '收到卸载回执', 'gate.check': '合并门禁检查' }
const headers = () => ({ 'X-Approval-CSRF': csrf })
async function task(fn) { if (busy.value) return; busy.value = true; error.value = ''; try { await fn() } catch (e) { error.value = e.response?.data?.error || e.message || '操作未完成'; ElMessage.error(error.value) } finally { busy.value = false } }
async function load() {
  policies.value = (await api.get('/approval-rules/policies')).data.items
  nativeInstances.value = (await api.get('/approval-rules/native-governance')).data.items
}
async function probeNative(item) { await task(async () => { const value = (await api.get(`/approval-rules/native-governance/${item.server_id}`)).data; const index = nativeInstances.value.findIndex(row => row.server_id === item.server_id); if (index >= 0) nativeInstances.value.splice(index, 1, value) }) }
function openCreate() { generation++; orgId.value = null; organizations.value = []; repositories.value = []; teams.value = []; form.value = { server_id: null, repo_id: null, rule: { name: '研发审批规则', total: 3, branches: ['main'], groups: [{ team_id: null, required: 1 }, { team_id: null, required: 1 }], reset_on_push: true } }; editing.value = true }
async function edit(row) {
  ++generation
  form.value = JSON.parse(JSON.stringify(row)); orgId.value = row.owner_id
  // 刷新后候选列表为空，仍需明确回显被编辑的组织、仓库和团队。
  organizations.value = [{ id: row.owner_id, name: row.repo_name.split('/')[0] }]
  repositories.value = [{ id: row.repo_id, full_name: row.repo_name, manageable: true }]
  teams.value = row.preview.groups?.map(g => ({ id: g.team_id, name: g.name })) || row.rule.groups.map(g => ({ id: g.team_id, name: '团队 ' + g.team_id }))
  editing.value = true
  await searchTeams('')
}
async function changeServer() {
  const seq = ++generation
  orgId.value = null
  form.value.repo_id = null
  form.value.rule.groups = [{ team_id: null, required: 1 }, { team_id: null, required: 1 }]
  organizations.value = []; repositories.value = []; teams.value = []
  await task(async () => {
    const r = await api.get('/repository-policies/organizations', { params: { server_id: form.value.server_id, page: 1 } })
    if (seq === generation) organizations.value = r.data.items
  })
}
function orgParams() { const org = organizations.value.find(o => o.id === orgId.value); return { server_id: form.value.server_id, org_id: orgId.value, org: org?.name || form.value.repo_name?.split('/')[0] } }
async function changeOrg() { ++generation; form.value.repo_id = null; form.value.rule.groups = [{ team_id: null, required: 1 }, { team_id: null, required: 1 }]; teams.value = []; repositories.value = []; await Promise.all([searchRepos(''), searchTeams('')]) }
async function searchRepos(q) { if (!orgId.value) return; const seq = generation, query = ++repoRequest; try { const r = await api.get('/repository-policies/repositories', { params: { ...orgParams(), q, limit: 100 } }); if (seq === generation && query === repoRequest) repositories.value = r.data.items } catch (e) { error.value = e.response?.data?.error || '仓库查询失败' } }
async function searchTeams(q) { if (!orgId.value) return; const seq = generation, query = ++teamRequest; try { const r = await api.get('/repository-policies/teams', { params: { ...orgParams(), q } }); if (seq === generation && query === teamRequest) teams.value = r.data.items } catch (e) { error.value = e.response?.data?.error || '团队查询失败' } }
function changeRepo() { const repo = repositories.value.find(r => r.id === form.value.repo_id); if (repo) form.value.rule.branches = [repo.default_branch] }
async function save() { await task(async () => { const f = form.value; if (!f.server_id || !f.repo_id || f.rule.groups.some(g => !g.team_id)) throw new Error('请选择实例、仓库和所有必需团队'); if (!f.rule.name?.trim()) throw new Error('请输入规则名称'); await api({ method: f.id ? 'put' : 'post', url: '/approval-rules/policies' + (f.id ? '/' + f.id : ''), headers: headers(), data: f }); editing.value = false; await load(); ElMessage.success('规则已保存，原生配置未修改') }) }
async function preview(row) { await task(async () => { previewData.value = (await api.post(`/approval-rules/policies/${row.id}/preview`, {}, { headers: headers() })).data; previewing.value = true; await load() }) }
function openBundle(row) { selected.value = row; bundling.value = true }
async function downloadBundle() { await task(async () => { let r; try { r = await api.post(`/approval-rules/policies/${selected.value.id}/bundle`, { mode: 'shadow', gate_url: gateUrl.value }, { headers: headers(), responseType: 'blob' }) } catch (e) { if (e.response?.data instanceof Blob) { const text = await e.response.data.text(); throw new Error(JSON.parse(text).error) } throw e } const url = URL.createObjectURL(r.data); const a = document.createElement('a'); a.href = url; a.download = `审批组件-${selected.value.id}.zip`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); bundling.value = false; await load() }) }
async function downloadNativeUninstall(row) { const value = await ElMessageBox.prompt('输入Gitea主机可访问的Manager回执地址，必须以 /api/approval-internal 结尾', '导出专用旧Hook卸载包', { inputValue: gateUrl.value }); gateUrl.value = value.value; await task(async () => { let r; try { r = await api.post(`/approval-rules/policies/${row.id}/native-uninstall-bundle`, { gate_url: gateUrl.value }, { headers: headers(), responseType: 'blob' }) } catch (e) { if (e.response?.data instanceof Blob) { const text = await e.response.data.text(); throw new Error(JSON.parse(text).error) } throw e } const url=URL.createObjectURL(r.data); const a=document.createElement('a'); a.href=url; a.download=`原生迁移卸载-${row.id}.zip`; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000); ElMessage.success('专用包已生成；在Gitea宿主机解压并执行包内脚本') }) }
function openStatus(row) { selected.value = row; prStatus.value = null; checkedExpired.value = false; checking.value = true }
async function checkPR() { prStatus.value = null; checkedExpired.value = false; clearTimeout(statusExpiry); const seq = ++statusRequest, pid = selected.value.id, number = prNumber.value; await task(async () => { const value = (await api.get(`/approval-rules/policies/${pid}/status/${number}`)).data; if (seq !== statusRequest || !checking.value || number !== prNumber.value) return; if (value.valid_until * 1000 <= Date.now()) { checkedExpired.value = true; return } prStatus.value = value; statusExpiry = setTimeout(() => { prStatus.value = null; checkedExpired.value = true }, Math.max(0, value.valid_until * 1000 - Date.now())) }) }
watch([checking, prNumber], () => { ++statusRequest; prStatus.value = null; checkedExpired.value = false; clearTimeout(statusExpiry) })
onUnmounted(() => { ++statusRequest; clearTimeout(statusExpiry) })
async function history(row) { await task(async () => { auditRows.value = (await api.get(`/approval-rules/policies/${row.id}/audit`)).data.items; showHistory.value = true }) }
async function nativePreview(row) { await task(async () => { nativePolicy.value = row; nativePlan.value = (await api.get(`/approval-rules/policies/${row.id}/native-import`)).data; showNative.value = true }) }
async function applyNative() { await task(async () => { const r = await api.post(`/approval-rules/policies/${nativePolicy.value.id}/native-import`, { rules: nativePlan.value.rules }, { headers: headers() }); nativePlan.value.native_url = r.data.native_url; showNative.value = false; await load(); ElMessage.success(r.data.legacy_hook_removal_pending ? '原生强制链路已生效，请用原接入包卸载旧门禁' : '原生规则、设置和强制链路已回读确认') }) }
async function disable(row) { let reason; try { const r = await ElMessageBox.prompt('记录停用原因。页面卸载不会代替门禁停用；申请后仍需卸载受管 Hook。', '停用审批门禁', { inputValidator: v => !!v?.trim() || '请输入原因' }); reason = r.value } catch { return } await task(async () => { await api.post(`/approval-rules/policies/${row.id}/disable`, { revision: row.revision, reason }, { headers: headers() }); await load(); ElMessage.info('停用申请已记录，请按安装包说明卸载门禁') }) }
onMounted(() => task(async () => { csrf = (await api.get('/approval-rules/csrf')).data.token; servers.value = (await api.get('/repository-policies/servers')).data; await load() }))
</script>

<style scoped>
.approval-page { display: grid; gap: 18px; min-width: 0; }
.approval-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.approval-heading h2 { margin: 0 0 8px; }
.approval-heading p, .approval-muted { color: var(--text-secondary, #667085); line-height: 1.6; }
.approval-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.approval-actions .el-button + .el-button { margin-left: 0; }
.approval-form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.approval-page :deep(.el-select) { width: 100%; min-width: 0; }
.approval-groups { display: grid; gap: 10px; width: 100%; }
.approval-group { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
.approval-group :deep(.el-select) { flex: 1 1 200px; }
.approval-audit { overflow-wrap: anywhere; white-space: pre-wrap; }
@media (max-width: 600px) { .approval-form-grid { grid-template-columns: minmax(0, 1fr); } }
</style>
