<template>
  <main class="mapping-page" v-loading="!mapping">
    <header class="page-heading">
      <div><h2>人员匹配确认</h2><p>{{ mapping?.source_name }} → {{ mapping?.server_name }}<span v-if="mapping?.target"> · {{ mapping.target.path }}</span></p></div>
      <el-button :disabled="busy || processing" @click="change('refresh')">刷新目标账号</el-button>
    </header>
    <el-alert v-if="pollError || mapping?.error" :title="mapping?.error || pollError" type="error" show-icon :closable="false" />
    <el-alert v-if="mapping?.stale" title="来源、盘点或目标连接已变化，请返回迁移来源重新建立匹配。" type="warning" show-icon :closable="false" />
    <el-alert v-if="processing" title="正在后台读取并核对完整账号名单，请稍候。离开页面不会中断任务。" type="info" show-icon :closable="false" />
    <section class="progress-surface">
      <h3>确认进度</h3>
      <div class="progress-line"><span>已确认 <strong>{{ stats.confirmed }}</strong> / {{ stats.total }} 项</span><el-progress :percentage="percentage" :stroke-width="10" :format="value => `${value}%`" /></div>
      <p>每项都需要确认处理方式：绑定目标账号，或保留来源身份。有效权限规则必须绑定可用账号。</p>
      <div class="resolution-summary"><span>已映射 {{ stats.mapped || 0 }}</span><span>保留来源身份 {{ stats.retained || 0 }}</span><button type="button" class="text-action" @click="filter = 'rules'">权限待处理 {{ stats.rules || 0 }}</button></div>
      <div class="filters" role="tablist" aria-label="人员匹配状态">
        <button v-for="item in filters" :key="item.key" type="button" role="tab" :aria-selected="filter === item.key" :class="['filter', { active: filter === item.key }]" @click="filter = item.key">{{ item.label }} <span>{{ stats[item.count] }}</span></button>
      </div>
    </section>
    <div class="mapping-workspace">
      <section class="people-panel" aria-label="来源人员名单">
        <div v-if="filter === 'automatic'" class="bulk-section">
          <div class="bulk-heading"><h3>自动匹配</h3><span>{{ stats.automatic_pending }} 项待确认</span></div>
          <el-button class="confirm-all" type="primary" plain :disabled="!stats.automatic_pending || busy || processing || mapping?.stale" :loading="bulkBusy" @click="confirmAll">确认全部自动匹配（{{ stats.automatic_pending }}）</el-button>
          <p>全部待确认自动匹配项，不受筛选、分页和勾选影响。</p>
        </div>
        <div class="search-box"><el-input v-model="search" placeholder="搜索姓名或来源账号" aria-label="搜索来源人员" clearable><template #prefix><el-icon><Search /></el-icon></template></el-input></div>
        <div class="selection-scope"><el-checkbox :model-value="pageChecked" :indeterminate="pagePartial" :disabled="busy || processing || !pageRows.length" @change="togglePage">本页全选</el-checkbox><el-button link type="primary" :disabled="busy || processing || !mapping?.selectable_total" @click="selectFiltered">选择全部筛选结果</el-button></div>
        <el-empty v-if="mapping && !mapping.rows.length" :description="processing ? '等待匹配结果' : '没有符合条件的人员'" :image-size="64" />
        <div class="people-list">
        <div v-for="person in mapping?.rows || []" :key="person.key" class="person-row" :class="{ selected: activeKey === person.key }">
          <el-checkbox :model-value="isPicked(person)" :disabled="person.confirmed || busy || processing" :aria-label="`勾选 ${person.source.name || person.source.login}`" @change="checked => togglePerson(person, checked)" />
          <button type="button" class="person-detail-button" :aria-current="activeKey === person.key ? 'true' : undefined" :aria-label="`查看 ${person.source.name || person.source.login} 的匹配详情`" @click="selectPerson(person)">
          <span class="person-name"><strong>{{ person.source.name || person.source.login }}</strong><small>{{ person.source.login }}</small></span>
          <span class="row-meta"><span class="role-label">{{ person.roles.join('、') }}</span><el-tag :type="person.resolution === 'source_identity' ? 'info' : person.confirmed ? 'success' : tones[person.match_status]" size="small">{{ person.resolution === 'source_identity' ? '保留来源身份' : person.confirmed ? '已确认' : person.manual ? '手动调整 · 待确认' : labels[person.match_status] }}</el-tag></span>
          <el-icon><ArrowRight /></el-icon>
          </button>
        </div>
        </div>
        <div v-if="selectedCount" class="selected-actions"><div><strong>{{ selectionMode === 'filtered' ? '已选择全部筛选结果' : '已跨页选择' }} {{ selectedCount }} 项</strong><el-button link type="primary" @click="clearSelection">取消选择</el-button></div><el-button type="primary" :disabled="busy || processing || selectedInvalid > 0 || mapping?.stale" @click="confirmSelected">确认选中人员（{{ selectedCount }}）</el-button><p>{{ selectedInvalid ? `其中 ${selectedInvalid} 项尚无有效目标账号，请先处理后确认。` : '仅确认勾选项，按各自目标账号保存。' }}</p></div>
        <div class="list-pagination"><el-pagination v-model:current-page="page" :page-size="50" :total="mapping?.filtered_total || 0" layout="prev, pager, next" :pager-count="5" small /></div>
      </section>
      <section class="detail-panel" aria-label="人员匹配详情">
        <template v-if="selected">
          <el-alert v-if="selected.resolution === 'source_identity'" title="已保留来源身份，未绑定目标账号。可在下方重新选择账号，选择后需要重新确认。" type="info" show-icon :closable="false" />
          <el-alert v-else-if="selected.critical && !selected.confirmed" title="涉及有效权限或正式评审要求，必须指定有效目标账号，不能使用历史身份代替。" type="warning" show-icon :closable="false" />
          <el-alert v-if="selected.resolution !== 'source_identity'" :title="selected.reason" :type="selected.confirmed ? 'success' : selected.match_status === 'automatic' ? 'info' : 'warning'" show-icon :closable="false" />
          <dl class="source-facts">
            <div><dt>来源姓名</dt><dd>{{ selected.source.name || '未提供' }}</dd></div><div><dt>来源账号</dt><dd>{{ selected.source.login }}</dd></div>
            <div><dt>来源编号</dt><dd>{{ selected.source.id || '规则主体' }}</dd></div><div><dt>邮箱</dt><dd>{{ selected.source.email || '未公开' }}</dd></div><div><dt>用途</dt><dd>{{ selected.roles.join('、') }}</dd></div>
          </dl>
          <h3>{{ selected.kind === 'person' ? '候选目标账号' : '目标账号名单' }}</h3>
          <div class="candidate-table">
            <el-table :data="selected.candidates" empty-text="未找到候选，请搜索目标已有账号" row-key="id">
              <el-table-column label="选择" width="65"><template #default="{ row }"><el-checkbox :model-value="chosenIds.includes(row.id)" :disabled="busy || processing || !row.eligible" :aria-label="`选择 ${row.login}`" @change="checked => toggleCandidate(row.id, checked)" /></template></el-table-column>
              <el-table-column label="姓名" min-width="90"><template #default="{ row }">{{ row.name || row.login }}</template></el-table-column>
              <el-table-column prop="login" label="账号" min-width="115" /><el-table-column prop="email" label="邮箱" min-width="180" />
              <el-table-column label="状态" min-width="120"><template #default="{ row }"><span v-if="row.eligible">可选择</span><span v-else class="unavailable">账号不可用或权限不足</span></template></el-table-column>
            </el-table>
          </div>
          <h3>搜索其他目标账号</h3>
          <el-select :model-value="selected.kind === 'person' ? chosenIds[0] : chosenIds" :multiple="selected.kind !== 'person'" :multiple-limit="100" filterable remote clearable reserve-keyword :remote-method="searchAccounts" :loading="searching" :disabled="busy || processing" placeholder="输入目标账号、姓名或邮箱" aria-label="选择目标已有账号" class="account-select" @change="value => choose(Array.isArray(value) ? value : value ? [value] : [])">
            <el-option v-for="user in accountOptions" :key="user.id" :value="user.id" :label="`${user.name || user.login} · ${user.login}${user.email ? ' · ' + user.email : ''}`" :disabled="user.eligible === false" />
          </el-select>
          <p class="muted">{{ selected.kind === 'person' ? '单个人员只能对应一个目标账号。' : '团队或规则角色可选择多个目标账号，最多 100 个。' }}{{ selected.critical ? '目标账号缺失时，请先在目标端准备账号及权限，再刷新列表。' : '不需要绑定目标账号的歧义或未匹配人员，可统一保留来源身份。' }}</p>
          <div class="detail-actions"><el-button :disabled="busy || processing" @click="nextPerson">稍后处理</el-button><el-button type="primary" :loading="busy && !bulkBusy" :disabled="!chosenIds.length || busy || processing || mapping?.stale" @click="confirmOne">确认并检查下一位<el-icon><ArrowRight /></el-icon></el-button></div>
          <p class="muted">只保存人员映射，不创建账号，也不修改目标权限。修改已确认项后必须重新确认。</p>
        </template>
        <el-empty v-else description="选择左侧人员查看详情" :image-size="80" />
      </section>
    </div>
    <footer class="mapping-footer">
      <div><strong>{{ stats.total - stats.confirmed }}</strong> 项尚未确认<p>{{ mapping?.ready ? '全部处理方式已确认，请先预迁移与检查。' : '确认进度达到 100% 后可继续。' }}</p></div>
      <div class="footer-actions"><el-button @click="saveDraft">保存草稿</el-button><el-button :disabled="!mapping?.retention_preview?.count || busy || processing || mapping?.stale" @click="openRetention">剩余人员保留来源身份</el-button><el-button type="primary" :disabled="!mapping?.ready || busy || processing || !!createdRun" @click="execute">预迁移与检查<el-icon><ArrowRight /></el-icon></el-button></div>
    </footer>
    <el-dialog v-model="retentionDialog" title="保留来源身份" width="min(620px, 94vw)" :close-on-click-modal="false">
      <p>仅处理尚未确认、未人工改选的歧义和未匹配人员。已确认映射及手动选择保持不变。</p>
      <div class="retention-counts"><el-tag type="success" size="large">{{ retentionReview?.count || 0 }} 项可保留来源身份</el-tag><el-tag type="warning" size="large">{{ retentionReview?.blocked || 0 }} 项权限仍待处理</el-tag></div>
      <dl class="retention-facts"><div><dt>历史署名</dt><dd>保留来源姓名、账号和稳定编号，Git 提交不改写。</dd></div><div><dt>负责人 / 评审请求</dt><dd>保留来源记录，不关联目标账号，不产生对应的目标待办或通知。</dd></div><div><dt>账号与权限</dt><dd>不创建账号、不新增权限；有效权限和正式评审要求必须另行映射。</dd></div></dl>
      <el-alert v-if="retentionReview?.remaining" :title="`保留后仍有 ${retentionReview.remaining} 项待处理，不会直接放行迁移。`" type="warning" :closable="false" show-icon />
      <el-alert v-if="retentionReview?.revision !== mapping?.revision" title="名单已变化，请关闭后重新查看影响范围。" type="warning" :closable="false" />
      <el-checkbox v-model="retentionAcknowledged" class="retention-ack">我已了解上述影响，确认保留来源身份</el-checkbox>
      <template #footer><el-button @click="retentionDialog = false">返回逐项处理</el-button><el-button type="primary" :disabled="!retentionAcknowledged || retentionReview?.revision !== mapping?.revision || busy || processing" @click="retainUnmatched">确认保留 {{ retentionReview?.count }} 项</el-button></template>
    </el-dialog>
  </main>
</template>

<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Search, ArrowRight } from '@element-plus/icons-vue'
import { api } from '../api'
import { useOperationPolling } from '../composables/useOperationPolling'
const router = useRouter()
const route = useRoute(), mapping = ref(null), filter = ref('all'), search = ref(''), page = ref(1)
const activeKey = ref(''), selected = computed(() => mapping.value?.rows.find(r => r.key === activeKey.value))
const chosenIds = ref([]), accountOptions = ref([]), busy = ref(false), bulkBusy = ref(false), searching = ref(false)
const createdRun = ref(null)
const retentionDialog = ref(false), retentionAcknowledged = ref(false), retentionReview = ref(null)
const selectionMode = ref('keys'), picked = ref({}), excluded = ref({})
const processing = computed(() => ['queued', 'running'].includes(mapping.value?.status))
const stats = computed(() => mapping.value?.stats || { total: 0, confirmed: 0, automatic: 0, ambiguous: 0, unmatched: 0, automatic_pending: 0 })
const percentage = computed(() => stats.value.total ? stats.value.confirmed === stats.value.total ? 100 : Math.min(99.9, Math.round(stats.value.confirmed / stats.value.total * 1000) / 10) : mapping.value?.ready ? 100 : 0)
const filters = [{ key: 'all', label: '全部', count: 'total' }, { key: 'automatic', label: '自动匹配', count: 'automatic' }, { key: 'ambiguous', label: '有歧义', count: 'ambiguous' }, { key: 'unmatched', label: '未匹配', count: 'unmatched' }, { key: 'confirmed', label: '已确认', count: 'confirmed' }]
filters.push({ key: 'retained', label: '保留来源身份', count: 'retained' }, { key: 'rules', label: '权限待处理', count: 'rules' })
const pageRows = computed(() => (mapping.value?.rows || []).filter(r => !r.confirmed))
const selectedCount = computed(() => selectionMode.value === 'filtered' ? Math.max(0, (mapping.value?.selectable_total || 0) - Object.keys(excluded.value).length) : Object.keys(picked.value).length)
const selectedInvalid = computed(() => selectionMode.value === 'filtered' ? Math.max(0, (mapping.value?.selectable_total || 0) - (mapping.value?.confirmable_total || 0) - Object.values(excluded.value).filter(v => !v).length) : Object.values(picked.value).filter(v => !v).length)
const pageChecked = computed(() => pageRows.value.length > 0 && pageRows.value.every(isPicked))
const pagePartial = computed(() => !pageChecked.value && pageRows.value.some(isPicked))
const labels = { automatic: '自动匹配', ambiguous: '有歧义', unmatched: '未匹配' }, tones = { automatic: 'success', ambiguous: 'warning', unmatched: 'danger' }
const endpoint = () => `/git-migrations/account-mappings/${route.params.mappingId}`
let accountRequest = 0, searchTimer
const { refresh, error: pollError } = useOperationPolling(async config => {
  const key = `${endpoint()}|${filter.value}|${search.value}|${page.value}`
  const response = await api.get(endpoint(), { ...config, params: { status: filter.value, q: search.value, page: page.value } })
  return { key, data: response.data }
}, result => {
  if (result.key !== `${endpoint()}|${filter.value}|${search.value}|${page.value}`) return
  const changed = result.data.revision !== mapping.value?.revision
  if (changed) clearSelection()
  mapping.value = result.data
  const item = result.data.rows.find(r => r.key === activeKey.value) || result.data.rows[0]
  if (item) {
    if (changed || item.key !== activeKey.value) selectPerson(item)
  } else { activeKey.value = ''; chosenIds.value = []; accountOptions.value = []; searching.value = false }
}, () => processing.value)
watch([filter, page], () => { activeKey.value = ''; refresh() })
watch(filter, () => { clearSelection(); page.value = 1 })
watch(search, () => { clearSelection(); clearTimeout(searchTimer); searchTimer = setTimeout(() => { page.value = 1; refresh() }, 250) })
watch(() => route.params.mappingId, () => { clearSelection(); activeKey.value = ''; createdRun.value = null; retentionDialog.value = false; refresh() })
onUnmounted(() => { clearTimeout(searchTimer); accountRequest++ })
function clearSelection() { selectionMode.value = 'keys'; picked.value = {}; excluded.value = {} }
function isPicked(person) { return !person.confirmed && (selectionMode.value === 'filtered' ? !(person.key in excluded.value) : person.key in picked.value) }
function togglePerson(person, checked) {
  if (person.confirmed) return
  if (selectionMode.value === 'filtered') { if (checked) delete excluded.value[person.key]; else excluded.value[person.key] = person.can_confirm }
  else if (checked) picked.value[person.key] = person.can_confirm
  else delete picked.value[person.key]
}
function togglePage(checked) { pageRows.value.forEach(person => togglePerson(person, checked)) }
function selectFiltered() { clearSelection(); selectionMode.value = 'filtered' }
async function confirmSelected() {
  const selection = selectionMode.value === 'filtered' ? { mode: 'filtered', status: filter.value, query: search.value, excluded: Object.keys(excluded.value) } : { mode: 'keys', keys: Object.keys(picked.value) }
  if (await change('confirm_selected', { selection })) ElMessage.success('已提交所选人员，正在后台逐项核对')
}
function openRetention() { retentionReview.value = { ...mapping.value.retention_preview, revision: mapping.value.revision }; retentionAcknowledged.value = false; retentionDialog.value = true }
async function retainUnmatched() {
  if (await change('retain_unmatched', { impact_hash: retentionReview.value.impact_hash, acknowledged: retentionAcknowledged.value })) { retentionDialog.value = false; ElMessage.success('所选范围已保留来源身份，有效权限例外仍需处理') }
}
function selectPerson(person) { activeKey.value = person.key; chosenIds.value = [...person.selected_ids]; accountOptions.value = [...new Map([...person.selected, ...person.candidates].map(u => [u.id, u])).values()]; accountRequest++; searching.value = false }
async function change(action, extra = {}) {
  if (!mapping.value || busy.value || processing.value) return
  busy.value = true
  try { await api.patch(endpoint(), { revision: mapping.value.revision, action, ...extra }); await refresh(); return true }
  catch { await refresh(); return false }
  finally { busy.value = false }
}
async function choose(ids) { await change('select', { key: activeKey.value, target_ids: ids }) }
async function toggleCandidate(id, checked) { await choose(checked ? selected.value.kind === 'person' ? [id] : [...new Set([...chosenIds.value, id])] : chosenIds.value.filter(v => v !== id)) }
async function searchAccounts(query) {
  const ticket = ++accountRequest, key = activeKey.value
  searching.value = true
  try { const result = await api.get(endpoint() + '/accounts', { params: { q: query, key } }); if (ticket === accountRequest && key === activeKey.value) accountOptions.value = [...new Map([...(selected.value?.selected || []), ...result.data.accounts].map(u => [u.id,u])).values()] }
  finally { if (ticket === accountRequest) searching.value = false }
}
async function confirmAll() { bulkBusy.value = true; try { if (await change('confirm_automatic')) ElMessage.success('已提交整批确认，正在后台核对全部自动匹配项') } finally { bulkBusy.value = false } }
function nextPerson() { const rows = mapping.value?.rows || [], index = rows.findIndex(r => r.key === activeKey.value); const next = rows.slice(index + 1).find(r => !r.confirmed) || rows.find(r => !r.confirmed && r.key !== activeKey.value); if (next) selectPerson(next); else if (page.value * 50 < mapping.value.filtered_total) page.value++ }
async function confirmOne() { if (await change('confirm', { key: activeKey.value })) nextPerson() }
async function saveDraft() { await refresh(); if (!pollError.value) ElMessage.success('映射选择与确认状态已保存，可稍后继续') }
const requestKey = crypto.randomUUID().replaceAll('-', '')
async function execute() {
  if (!mapping.value?.ready || createdRun.value) return
  busy.value = true
  try {
    const rows = (await api.get('/git-migrations/preflights')).data
    const existing = rows.find(row => row.source_id === mapping.value.source_id && ['queued','running','ready','blocked','interrupted'].includes(row.status))
    if (existing) { await router.push(`/git-migrations/preflights/${existing.id}`); return }
    const result = await api.post('/git-migrations/preflights', { inventory_id: mapping.value.inventory_id, server_id: mapping.value.server_id, group_id: mapping.value.group_id, mapping_id: mapping.value.id, request_key: requestKey }); createdRun.value = result.data.id; await router.push(`/git-migrations/preflights/${result.data.id}`)
  }
  finally { busy.value = false }
}
</script>

<style scoped>
.mapping-page { display: grid; gap: 14px; min-width: 0; }
.page-heading,.progress-line,.bulk-heading,.detail-actions,.mapping-footer,.footer-actions,.selection-scope,.retention-counts { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; min-width: 0; }
h2 { font-size: 30px; margin: 0 0 8px; color: #172333; } h3 { font-size: 17px; margin: 0; color: #172333; }
p { margin: 8px 0 0; color: #697b91; font-size: 13px; line-height: 1.6; }
.progress-surface,.people-panel,.detail-panel,.mapping-footer { background: rgba(255,255,255,.9); border: 1px solid #dce7f2; border-radius: 10px; min-width: 0; }
.progress-surface { padding: 14px; } .progress-line { margin-top: 12px; } .progress-line strong,.mapping-footer strong { color: #1765be; font-size: 23px; }
.progress-line :deep(.el-progress) { flex: 1 1 300px; min-width: 0; } .filters { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }
.filter { display: inline-flex; gap: 8px; align-items: center; padding: 8px 14px; border: 1px solid transparent; background: #f0f4f8; border-radius: 9px; color: #58677c; cursor: pointer; font: inherit; font-size: 14px; }
.filter.active { background: #1765be; color: white; } .filter:focus-visible,.person-row:focus-visible { outline: 2px solid #1765be; outline-offset: 2px; }
.mapping-workspace { display: grid; grid-template-columns: minmax(290px, 36%) minmax(0, 1fr); gap: 16px; }
.people-panel { overflow: hidden; display: flex; flex-direction: column; } .people-list { max-height: 200px; min-height: 130px; overflow-y: auto; } .bulk-section { padding: 16px 16px 0; } .bulk-heading { gap: 8px; } .search-box { padding: 16px; }
.confirm-all { width: 100%; margin-top: 12px; }.bulk-heading > span { color: #1765be; font-size: 13px; }
.selection-scope { padding: 0 16px 10px; gap: 8px; }.selection-scope :deep(.el-button) { white-space: normal; height: auto; }
.person-row { display: flex; align-items: center; gap: 10px; width: 100%; min-width: 0; padding: 0 12px; border-top: 1px solid #edf1f6; border-left: 3px solid transparent; background: transparent; text-align: left; font: inherit; }
.person-detail-button { display: flex; flex: 1; align-items: center; gap: 10px; min-width: 0; padding: 16px 0; border: 0; background: transparent; text-align: left; font: inherit; cursor: pointer; }.person-detail-button:focus-visible { outline: 2px solid #1765be; }
.selected-actions { padding: 10px 16px; display: grid; gap: 6px; background: #edf6ff; }.selected-actions > div { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; font-size: 13px; }.selected-actions > p { margin: 0; }
.resolution-summary { display: flex; gap: 18px; align-items: center; flex-wrap: wrap; margin-top: 6px; color: #62748b; font-size: 13px; }.text-action { border: 0; padding: 0; color: #b45309; cursor: pointer; background: transparent; font: inherit; }
.retention-counts { justify-content: flex-start; margin: 18px 0; }.retention-facts { display: grid; gap: 14px; }.retention-facts > div { display: grid; grid-template-columns: 115px 1fr; gap: 12px; }.retention-facts dt { font-size: 14px; }.retention-ack { margin-top: 18px; white-space: normal; height: auto; }.retention-ack :deep(.el-checkbox__label) { white-space: normal; }
.person-row.selected { background: #eaf4ff; border-left-color: #1765be; } .person-name { display: grid; gap: 8px; min-width: 0; flex: 1; overflow-wrap: anywhere; } .person-name small { color: #687b94; }
.row-meta { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: 8px; min-width: 0; max-width: 48%; } .role-label { color: #5f6e83; font-size: 12px; }
.list-pagination { display: flex; justify-content: center; padding: 8px 4px; border-top: 1px solid #edf1f6; } .detail-panel { padding: 16px; display: flex; flex-direction: column; gap: 10px; }
.source-facts { display: grid; grid-template-columns: repeat(5,minmax(0,1fr)); gap: 12px; padding: 16px; margin: 0; border: 1px solid #e5ecf4; border-radius: 9px; }
dt { color: #6b7f98; font-size: 12px; margin-bottom: 8px; } dd { margin: 0; font-size: 14px; overflow-wrap: anywhere; } .candidate-table { min-width: 0; } .account-select { width: 100%; }
.detail-actions { justify-content: flex-end; margin-top: auto; } .detail-actions :deep(.el-button),.footer-actions :deep(.el-button) { display: inline-flex; gap: 8px; align-items: center; }
.unavailable { color: #b45309; font-size: 12px; }.mapping-footer { padding: 18px 22px; }.mapping-footer p { margin-top: 2px; }.footer-actions { justify-content: flex-end; }
@media (max-width: 1100px) { .mapping-workspace { grid-template-columns: minmax(260px, 34%) minmax(0,1fr); }.row-meta { max-width: 40%; }.source-facts { grid-template-columns: repeat(2,minmax(0,1fr)); } }
@media (max-width: 760px) { h2 { font-size: 25px; }.mapping-workspace { grid-template-columns: minmax(0,1fr); }.progress-line :deep(.el-progress) { flex-basis: 100%; }.source-facts { grid-template-columns: repeat(2,minmax(0,1fr)); }.mapping-footer { align-items: flex-start; }.footer-actions { width: 100%; }.detail-actions { flex-wrap: wrap; }.filter { padding: 8px 10px; }.progress-surface { padding: 14px; } }
</style>
