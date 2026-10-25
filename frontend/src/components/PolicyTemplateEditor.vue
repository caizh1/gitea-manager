<template>
  <div class="template-page">
    <fieldset class="template-controls" :disabled="busy || verifying">
    <div class="template-toolbar"><div><el-button link @click="close">返回策略模板</el-button><h3>{{ title }}</h3></div><el-button type="primary" :loading="busy" :disabled="!organization || needsConfirmation" @click="save">保存模板</el-button></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <section class="template-information glass-card">
      <h4>模板信息</h4>
      <el-form label-position="top" @submit.prevent="save">
        <div class="information-grid">
          <el-form-item label="模板名称" required><el-input v-model="form.name" aria-label="模板名称" maxlength="100" /></el-form-item>
          <el-form-item label="所属组织" required><PolicyOrganizationSelect v-if="canChooseOrganization" :model-value="organization" :server-id="serverId" @update:model-value="changeOrganization" /><span v-else>{{ organization?.name }}</span></el-form-item>
        </div>
        <el-form-item label="说明"><el-input v-model="form.description" aria-label="模板说明" maxlength="2000" /></el-form-item>
      </el-form>
      <p>Gitea 实例：{{ serverName }}<span v-if="form.source?.full_name"> · 规则来源：{{ form.source.full_name }}（导入时快照）</span></p>
      <p class="muted">模板独立保存。保存不会修改仓库；下发时逐仓库核验权限。用户候选默认来自组织成员，输入用户名可搜索实例中的其他账户。</p>
      <p v-if="!item?.organization && item" class="muted">此旧模板尚未归属组织。请选择组织并核验现有名单后保存，历史执行记录保留。</p>
      <el-alert v-if="copyAcross" title="复制到其他组织后，原团队需要重新选择。请确认推送、强推、批准和合并名单，再核验并保存。" type="warning" :closable="false" />
      <div v-if="clearedTeams.length" class="cleared-teams"><strong>需要重新选择的原团队</strong><p v-for="entry in clearedTeams" :key="entry">{{ entry }}</p></div>
      <div class="template-actions"><el-button :loading="verifying" :disabled="!organization || !form.rules.length || busy" @click="verify">核验名单</el-button><span>{{ needsConfirmation ? '名单待确认，暂不能保存和下发' : '名单身份将在保存和批量预览时核验' }}</span></div>
      <p class="muted">仓库的历史状态检查记录属于具体仓库，可在 Gitea 仓库分支设置中查看；不作为组织模板的候选数据。</p>
    </section>
    <section v-if="organization" class="rule-workspace">
      <div class="rule-list glass-card">
        <div class="template-toolbar"><h4>分支规则</h4><el-button :disabled="form.rules.length >= 50" @click="addRule">添加规则</el-button></div>
        <p class="muted">数字越小优先级越高。导入保留原值；排序不会改变规则内容。</p>
        <div v-for="(rule, index) in form.rules" :key="ruleKey(rule)" class="rule-list-item" :class="{ active: activeIndex === index }">
          <el-button link class="rule-name" @click="activeIndex = index">{{ rule.rule_name || '未命名规则' }}</el-button>
          <label class="priority-control"><span>优先级</span><el-input-number v-model="rule.priority" :aria-label="`${rule.rule_name || '未命名规则'} 优先级`" :min="1" :max="1000000" controls-position="" /></label>
          <el-button link type="danger" @click="removeRule(index)">移除</el-button>
        </div>
        <div class="template-actions"><el-button @click="sortRules">按优先级排序</el-button><el-button @click="openImport">从仓库导入</el-button></div>
      </div>
      <div v-if="activeRule" class="native-rule-panel">
        <h4 class="native-rule-heading">{{ activeRule.rule_name ? `分支「${activeRule.rule_name}」的保护规则` : '添加新规则' }}</h4>
        <form class="native-rule-body" @submit.prevent="save">
          <PolicyRuleEditor :key="ruleKey(activeRule)" :rule="activeRule" :server-id="serverId" :organization="organization" :identities="form.identities" />
          <div class="template-actions"><el-button native-type="submit" type="primary" :loading="busy" :disabled="needsConfirmation">保存规则</el-button><el-button @click="close">取消</el-button></div>
        </form>
      </div>
      <el-empty v-else description="添加新规则，或从本组织仓库导入。" />
    </section>
    <el-dialog append-to-body v-model="importVisible" title="从仓库导入保护规则" width="min(740px, 96vw)" :close-on-click-modal="false">
      <div class="template-actions"><el-select v-model="sourceRepoId" filterable remote :remote-method="sourceSearch.search" :loading="sourceState.loading" @visible-change="v => v && sourceSearch.open()" @change="clearImported" placeholder="搜索并选择源仓库" aria-label="选择源仓库" class="source-select">
        <el-option v-for="repo in sourceState.items" :key="repo.id" :value="repo.id" :label="repo.full_name" :disabled="!repo.manageable" />
        <template v-if="sourceState.hasMore || sourceState.error" #footer><div class="template-actions"><span>{{ sourceState.error }}</span><el-button link @click.stop="sourceState.error ? sourceSearch.retry() : sourceSearch.more()">{{ sourceState.error ? '重试查询' : '加载更多仓库' }}</el-button></div></template>
      </el-select><el-button :disabled="!sourceRepoId" :loading="importing" @click="readSource">读取规则</el-button></div>
      <el-checkbox-group v-model="importNames" class="import-list"><el-checkbox v-for="rule in imported?.rules || []" :key="rule.rule_name" :value="rule.rule_name">{{ rule.rule_name }}（优先级 {{ rule.priority }}）</el-checkbox></el-checkbox-group>
      <p class="muted">所选规则将加入当前模板；同名规则会替换编辑内容。来源仓库以后删除不影响模板使用。</p>
      <template #footer><el-button @click="importVisible = false">取消</el-button><el-button type="primary" :disabled="!importNames.length" @click="useImported">使用所选规则</el-button></template>
    </el-dialog>
    </fieldset>
  </div>
</template>
<script setup>
import { computed, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import { notifyApiError } from '../api/errors.js'
import PolicyOrganizationSelect from './PolicyOrganizationSelect.vue'
import PolicyRuleEditor from './PolicyRuleEditor.vue'
import { createCandidateSearch, teamFields, toApiRule, toEditorRule } from './policyForm'
const props = defineProps({ item: Object, copy: Boolean, copyAcross: Boolean, selectedOrganization: Object, serverId: Number, serverName: String, schema: Object, csrfToken: String })
const emit = defineEmits(['close', 'saved'])
const clone = value => JSON.parse(JSON.stringify(value))
const organization = ref(props.copyAcross || props.item && !props.item.organization ? null : clone(props.item?.organization || props.selectedOrganization || null))
const form = ref({ name: props.item ? props.item.name + (props.copy || props.copyAcross ? ' 副本' : '') : '', description: props.item?.description || '',
  rules: (props.item?.rules || []).map(r => toEditorRule(r, props.schema.list_fields)), identities: clone(props.item?.identities || { users: [], teams: [] }), source: clone(props.item?.source || {}) })
const editingId = props.item && !props.copy && !props.copyAcross ? props.item.id : null
const canChooseOrganization = computed(() => props.copyAcross || !props.item?.organization && !!props.item)
const title = computed(() => props.copyAcross ? '复制到其他组织' : editingId ? '编辑策略模板' : '创建策略模板')
const activeIndex = ref(0), busy = ref(false), verifying = ref(false), error = ref(''), needsConfirmation = ref(!!props.copyAcross || !!props.item && !props.item.organization), confirmed = ref(false), clearedTeams = ref([])
const activeRule = computed(() => form.value.rules[activeIndex.value])
const ruleKeys = new WeakMap(); let nextKey = 0, disposed = false, importSequence = 0
function ruleKey(rule) { if (!ruleKeys.has(rule)) ruleKeys.set(rule, ++nextKey); return ruleKeys.get(rule) }
function fail(e) { error.value = e.response?.data?.error || e.message || '操作失败'; notifyApiError(e, error.value) }
async function send(method, path, data) { return (await api.request({ method, url: '/repository-policies' + path, data, headers: { 'X-Policy-CSRF': props.csrfToken } })).data }
const original = JSON.stringify(form.value)
async function close() { if (busy.value || verifying.value) return; try { if (JSON.stringify(form.value) !== original) await ElMessageBox.confirm('离开后未保存的模板修改将丢失。', '返回策略模板', { confirmButtonText: '放弃修改', cancelButtonText: '继续编辑' }); emit('close') } catch {} }
async function changeOrganization(next) {
  if (organization.value?.id === next?.id) return
  const mismatched = form.value.rules.filter(r => teamFields.some(k => r[k]?.length) && r.team_org?.toLowerCase() !== next?.name?.toLowerCase())
  try {
    if (mismatched.length) await ElMessageBox.confirm('所选组织与原团队所属组织不同，将清除这些团队选择。请在各规则中明确选择新团队，不能按同名自动替换。', '重新选择团队', { confirmButtonText: '清除并重新选择', cancelButtonText: '取消' })
    if (disposed) return
    clearedTeams.value = [...new Set(mismatched.flatMap(r => teamFields.flatMap(k => (r[k] || []).map(name => `${r.rule_name}：${r.team_org} / ${name}`))))]
    mismatched.forEach(r => teamFields.forEach(k => { r[k] = [] }))
    if (organization.value?.id !== next?.id) form.value.identities.teams = []
    organization.value = next; form.value.rules.forEach(r => { r.team_org = next?.name || '' })
    needsConfirmation.value = true; confirmed.value = false; ++importSequence; importVisible.value = false
  } catch (e) { if (e !== 'cancel' && e !== 'close') fail(e) }
}
function addRule() {
  const rule = { rule_name: '', priority: Math.max(0, ...form.value.rules.map(r => r.priority)) + 1, required_approvals: 0, team_org: organization.value.name }
  props.schema.bool_fields.forEach(k => { rule[k] = false }); props.schema.list_fields.forEach(k => { rule[k] = k === 'status_check_contexts' ? '' : [] }); props.schema.text_fields.forEach(k => { rule[k] = '' })
  form.value.rules.push(rule); activeIndex.value = form.value.rules.length - 1
}
function removeRule(index) { form.value.rules.splice(index, 1); activeIndex.value = Math.max(0, Math.min(activeIndex.value, form.value.rules.length - 1)) }
function sortRules() { const current = activeRule.value; form.value.rules.sort((a, b) => a.priority - b.priority || a.rule_name.localeCompare(b.rule_name)); activeIndex.value = Math.max(0, form.value.rules.indexOf(current)) }
function payload() { return { server_id: props.serverId, organization: organization.value, rules: form.value.rules.map(toApiRule), identities: form.value.identities } }
async function verify() {
  verifying.value = true; error.value = ''
  const frozen = JSON.stringify(payload())
  try {
    const data = await send('post', '/identities/verify', { ...payload(), confirm_names: !!props.item && !props.item.organization })
    if (disposed || frozen !== JSON.stringify(payload())) throw new Error('核验期间配置已变化，请重新核验')
    const names = [...data.identities.users.map(x => `用户：${x.name}`), ...data.identities.teams.map(x => `团队：${data.organization.name} / ${x.name}`)]
    await ElMessageBox.confirm(names.length ? names.join('；') : '当前规则未指定用户或团队名单。', '确认模板名单', { confirmButtonText: '确认名单', cancelButtonText: '继续编辑', closeOnClickModal: false })
    if (disposed || frozen !== JSON.stringify(payload())) throw new Error('配置已变化，请重新核验')
    form.value.identities = data.identities; form.value.rules = data.rules.map(r => toEditorRule(r, props.schema.list_fields)); needsConfirmation.value = false; confirmed.value = true
    ElMessage.success('名单身份已核验；仓库适用权限仍在批量预览中检查')
  } catch (e) { if (e !== 'cancel' && e !== 'close') fail(e) } finally { verifying.value = false }
}
async function save() {
  if (!organization.value || needsConfirmation.value || busy.value || verifying.value) return
  busy.value = true; error.value = ''
  try {
    const data = await send(editingId ? 'put' : 'post', '/templates' + (editingId ? '/' + editingId : ''), { ...payload(), name: form.value.name, description: form.value.description,
      content_hash: props.item?.content_hash, source: form.value.source, copy_from_id: !editingId ? props.item?.id : undefined, confirm_identity_changes: confirmed.value })
    if (!disposed) { ElMessage.success('模板已保存；仓库规则未改变'); emit('saved', data) }
  } catch (e) { fail(e) } finally { busy.value = false }
}
const importVisible = ref(false), sourceRepoId = ref(null), sourceState = reactive({}), importing = ref(false), imported = ref(null), importNames = ref([])
const sourceSearch = createCandidateSearch({ state: sourceState, request: async (context, q, page, signal) => { const { data } = await api.get('/repository-policies/repositories', { params: { server_id: props.serverId, org: context.name, org_id: context.id, q, page, limit: 20 }, signal }); return { ...data, items: data.items.map(x => ({ ...x, name: x.full_name })), has_more: page*20 < data.total } } })
async function openImport() { clearImported(); sourceRepoId.value = null; sourceSearch.reset({ ...organization.value }); importVisible.value = true; await sourceSearch.open() }
function clearImported() { ++importSequence; imported.value = null; importNames.value = []; importing.value = false }
async function readSource() {
  const sequence = ++importSequence, id = sourceRepoId.value, oid = organization.value.id; importing.value = true; error.value = ''
  try { const { data } = await api.get(`/repository-policies/repositories/${id}/rules`, { params: { server_id: props.serverId, org: organization.value.name, org_id: oid } }); if (disposed || sequence !== importSequence || !importVisible.value || oid !== organization.value?.id) return; imported.value = data; importNames.value = data.rules.map(r => r.rule_name); if (!data.rules.length) ElMessage.info('源仓库没有保护规则') } catch (e) { if (sequence === importSequence) fail(e) } finally { if (sequence === importSequence) importing.value = false }
}
function useImported() {
  const rules = imported.value.rules.filter(r => importNames.value.includes(r.rule_name)), map = new Map(form.value.rules.map(r => [r.rule_name, r]))
  rules.forEach(r => map.set(r.rule_name, toEditorRule(r, props.schema.list_fields)))
  if (map.size > 50) return ElMessage.error('最多保存 50 条规则')
  for (const kind of ['users', 'teams']) {
    const refs = new Map((form.value.identities[kind] || []).map(x => [x.name.toLowerCase(), x]))
    // 已有同名绑定保留，若来源中出现重建身份，保存时会阻塞并要求明确处理。
    imported.value.identities[kind].forEach(x => { if (!refs.has(x.name.toLowerCase())) refs.set(x.name.toLowerCase(), x) })
    form.value.identities[kind] = [...refs.values()]
  }
  form.value.rules = [...map.values()]; form.value.source = imported.value.repo; activeIndex.value = 0; importVisible.value = false
}
onUnmounted(() => { disposed = true; ++importSequence; sourceSearch.dispose() })
</script>
<style scoped>
.template-page { min-width: 0; }
.template-controls { border: 0; padding: 0; margin: 0; min-width: 0; }
.template-toolbar, .template-actions { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; min-width: 0; }
.template-toolbar { justify-content: space-between; margin-bottom: 16px; }
.template-actions :deep(.el-button + .el-button) { margin-left: 0; }
.template-information, .rule-list { padding: 20px; margin-bottom: 22px; min-width: 0; }
.template-information h4 { margin-top: 0; }
.template-information p { overflow-wrap: anywhere; line-height: 1.6; }
.information-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px; }
.muted { color: #707070; font-size: 13px; line-height: 1.6; }
.rule-workspace { display: grid; grid-template-columns: minmax(240px, 290px) minmax(0, 1fr); align-items: start; gap: 24px; }
.rule-list-item { display: flex; flex-direction: column; align-items: stretch; gap: 8px; padding: 14px 8px; border-bottom: 1px solid #ddd; }
.rule-list-item.active { background: #eef5fc; }
.rule-list .template-actions { margin-top: 18px; }
.rule-name { white-space: normal; overflow-wrap: anywhere; justify-content: flex-start; height: auto; }
.priority-control { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.priority-control :deep(.el-input-number) { width: 150px; }
.native-rule-panel { border: 1px solid #d4d4d5; border-radius: 4px; background: white; min-width: 0; }
.native-rule-heading { margin: 0; padding: 14px 16px; background: #f0f0f0; border-bottom: 1px solid #d4d4d5; font-size: 16px; overflow-wrap: anywhere; }
.native-rule-body { padding: 0 16px 20px; }
.source-select { width: 420px; max-width: 100%; }
.import-list { display: flex; flex-direction: column; gap: 8px; margin: 20px 0; }
.cleared-teams { border-left: 3px solid #e6a23c; padding-left: 12px; margin: 16px 0; overflow-wrap: anywhere; }
.template-page :deep(.el-alert) { margin-bottom: 18px; }
@media (max-width: 1000px) { .rule-workspace { grid-template-columns: minmax(0, 1fr); } }
@media (max-width: 600px) { .information-grid { grid-template-columns: minmax(0, 1fr); gap: 0; } .template-information, .rule-list { padding: 12px; } .native-rule-body { padding: 0 12px 16px; } }
</style>
