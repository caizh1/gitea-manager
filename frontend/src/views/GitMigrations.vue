<template>
  <div class="git-migration-page">
    <header class="heading">
      <div><h2>GitLab / Gitea 来源</h2><p>连接来源并自动发现项目，保留原有群组与仓库层级。</p></div>
      <el-button type="primary" @click="editSource()"><el-icon><Plus /></el-icon><span>添加来源</span></el-button>
    </header>
    <el-alert title="迁移功能正在隔离验收。执行时会保留未完成范围，只有通过完整性检查的仓库才会交接到目标群组。" type="info" :closable="false" show-icon />
    <el-alert v-if="pollError" :title="pollError" type="warning" :closable="false" show-icon />
    <div class="workspace">
      <section class="surface sources">
        <div class="section-heading"><h3>来源实例</h3><el-tag :type="health.worker?.online ? 'success' : 'warning'">{{ health.worker?.online ? '执行器在线' : '执行器未在线' }}</el-tag></div>
        <el-empty v-if="loaded && !sources.length" description="添加 GitLab 或 Gitea 来源开始发现" :image-size="64" />
        <button v-for="source in sources" :key="source.id" class="source-row" :class="{ selected: source.id === selectedId }" :aria-pressed="source.id === selectedId" @click="selectSource(source.id)">
          <strong>{{ source.name }}</strong><span>{{ source.kind === 'gitlab' ? 'GitLab' : 'Gitea' }} · {{ source.archived ? '已停用' : '已登记' }}</span><small>{{ source.url }}</small>
        </button>
      </section>
      <section class="surface projects">
        <template v-if="selected">
          <div class="section-heading">
            <div><h3>{{ selected.name }}</h3><p>{{ selected.url }}</p></div>
            <div class="actions"><el-button :disabled="running" @click="editSource(selected)">编辑来源</el-button><el-button type="primary" :loading="saving || running" :disabled="!health.worker?.online || selected.archived" @click="discover">连接并发现项目</el-button></div>
          </div>
          <el-alert v-if="!health.worker?.online" title="来源执行器未在线，请先启动 Git 来源执行器。不会在浏览器请求中执行长时间发现。" type="warning" :closable="false" />
          <div v-if="history.length" class="history-line"><span>发现记录</span><el-select v-model="selectedDiscovery" aria-label="选择发现记录" @change="refresh"><el-option v-for="item in history" :key="item.id" :value="item.id" :label="`#${item.id} · ${label(item.status)}${item.stale ? ' · 来源已变化' : ''}`" /></el-select></div>
          <el-alert v-if="record?.error" :title="record.error" type="error" :closable="false" show-icon />
          <el-alert v-if="record?.stale" title="来源配置已变化，这是历史发现结果，请重新发现后使用。" type="warning" :closable="false" />
          <div v-if="record" class="record-line"><el-tag :type="tone(record.status)">{{ label(record.status) }}</el-tag><span>发现 #{{ record.id }}</span><span v-if="record.result?.version">{{ record.result.kind === 'gitlab' ? 'GitLab' : 'Gitea' }} {{ record.result.version }}</span><span v-if="record.status === 'succeeded'">{{ record.result.projects.length }} 个项目</span></div>
          <el-checkbox v-if="record?.status === 'succeeded'" v-model="captureCode">同时核验 Git 代码与历史 LFS（耗时较长）</el-checkbox>
          <el-input v-if="record?.status === 'succeeded'" v-model="search" placeholder="搜索仓库名称或完整路径" aria-label="搜索项目" clearable />
          <el-table v-if="record?.status === 'succeeded'" :data="visibleProjects" row-key="id" class="project-table">
            <el-table-column label="项目 / 原完整路径" min-width="210"><template #default="{ row }"><strong>{{ row.name }}</strong><small class="project-path">{{ row.path }}</small></template></el-table-column>
            <el-table-column prop="default_branch" label="默认分支" min-width="110"><template #default="{ row }">{{ row.default_branch || '空仓库 / 未设置' }}</template></el-table-column>
            <el-table-column label="可见性" min-width="90"><template #default="{ row }">{{ { private: '私有', internal: '内部', public: '公开' }[row.visibility] || '未确认' }}</template></el-table-column>
            <el-table-column label="状态" min-width="85"><template #default="{ row }">{{ row.archived ? '已归档' : '正常' }}</template></el-table-column>
            <el-table-column label="盘点" min-width="100"><template #default="{ row }"><el-button link type="primary" :disabled="record.stale || running || saving || !health.worker?.online" @click="inspectProject(row)">资料盘点</el-button></template></el-table-column>
          </el-table>
          <el-pagination v-if="filteredProjects.length > 50" v-model:current-page="page" :page-size="50" :total="filteredProjects.length" layout="prev, pager, next" />
          <p v-if="record?.status === 'succeeded'" class="note">发现成功仅表示项目列表读取完成。代码、协作资料、制品和 CI 历史仍需独立盘点及核验。</p>
          <section v-if="inventoryHistory.length" class="inventory-section">
            <div class="history-line"><strong>资料盘点记录</strong><el-select v-model="selectedInventory" aria-label="选择资料盘点记录" @change="refresh"><el-option v-for="item in inventoryHistory" :key="item.id" :value="item.id" :label="`#${item.id} · ${inventoryLabel(item.status)}`" /></el-select></div>
            <el-alert v-if="inventoryRecord?.error" :title="inventoryRecord.error" type="error" :closable="false" />
            <el-alert v-if="inventoryRecord?.stale" title="来源配置已变化，以下盘点结果仅供查看历史。" type="warning" :closable="false" />
            <template v-for="project in inventoryRecord?.result?.projects || []" :key="project.project.id">
              <h4>{{ project.project.path }} · {{ project.object_count }} 个元数据对象</h4>
              <p v-if="project.code_snapshot">代码引用核验完成，{{ project.code_snapshot.lfs_objects.length }} 个历史 LFS 对象；尚未写入目标。</p>
              <el-alert :title="project.code_snapshot ? 'Git 与 LFS 已完成来源侧核验；Wiki、附件、日志和制品内容尚未核验，也未证明源端停写与目标兼容。' : project.boundary" type="info" :closable="false" />
              <el-table :data="project.gaps" empty-text="本次调用没有发现读取缺口；仍不代表资料完整可迁移。"><el-table-column prop="category" label="待解决范围" min-width="130" /><el-table-column prop="reason" label="原因" min-width="240" /></el-table>
            </template>
          </section>
          <GitMigrationExecution :inventory="inventoryRecord" :source-id="selectedId" />
          <el-empty v-if="!record" description="点击“连接并发现项目”，自动读取来源中的项目列表" :image-size="80" />
        </template>
        <el-empty v-else description="选择或添加一个来源" :image-size="80" />
      </section>
    </div>
    <el-dialog v-model="dialog" :title="editingId ? '编辑来源' : '添加来源'" width="min(640px, 94vw)" :close-on-click-modal="false" @closed="form.secret = ''">
      <el-form label-position="top" @submit.prevent="save">
        <el-form-item label="来源名称"><el-input v-model="form.name" maxlength="200" placeholder="例如：研发 GitLab" /></el-form-item>
        <div class="form-grid"><el-form-item label="来源平台"><el-select v-model="form.kind"><el-option label="GitLab 社区版 / 企业版" value="gitlab" /><el-option label="开源 Gitea" value="gitea" /></el-select></el-form-item><el-form-item label="认证方式"><el-select v-model="form.auth_method" @change="form.secret = ''"><el-option label="管理员账号和密码" value="password" /><el-option label="访问令牌" value="token" /></el-select></el-form-item></div>
        <el-form-item label="实例连接地址"><el-input v-model="form.url" placeholder="https://git.example.com，不需要填写具体仓库地址" /></el-form-item>
        <el-form-item label="管理员账号"><el-input v-model="form.username" autocomplete="off" /></el-form-item>
        <el-form-item :label="form.auth_method === 'password' ? '管理员密码' : '访问令牌'"><el-input v-model="form.secret" type="password" show-password autocomplete="new-password" :placeholder="editingId ? '保持地址和账号不变时，留空保留已有凭据' : ''" /></el-form-item>
        <p class="note">凭据加密保存在 Manager。若源端启用多因素认证或禁用密码授权，请使用具备所需读取权限的管理员令牌。</p>
        <el-collapse><el-collapse-item title="自定义证书" name="ca"><el-form-item label="内部 CA 证书（可选）"><el-input v-model="form.ca_pem" type="textarea" :rows="4" placeholder="PEM 格式 CA 证书；默认校验系统信任链" /></el-form-item></el-collapse-item></el-collapse>
      </el-form>
      <template #footer><el-button :disabled="saving" @click="dialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="save">保存来源</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import GitMigrationExecution from '../components/GitMigrationExecution.vue'
import { ElMessage } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { api } from '../api'
import { useOperationPolling } from '../composables/useOperationPolling'

const sources = ref([]), health = ref({}), selectedId = ref(null), selectedDiscovery = ref(null)
const history = ref([]), record = ref(null), loaded = ref(false), saving = ref(false), dialog = ref(false), editingId = ref(null)
const search = ref(''), page = ref(1), captureCode = ref(true)
const inventoryHistory = ref([]), inventoryRecord = ref(null), selectedInventory = ref(null)
const form = reactive({ name: '', kind: 'gitlab', url: '', username: '', auth_method: 'password', secret: '', ca_pem: '' })
const selected = computed(() => sources.value.find(row => row.id === selectedId.value))
const running = computed(() => [...history.value, ...inventoryHistory.value].some(row => ['queued', 'running'].includes(row.status)))
const filteredProjects = computed(() => (record.value?.result?.projects || []).filter(row => `${row.name} ${row.path}`.toLowerCase().includes(search.value.toLowerCase())))
const visibleProjects = computed(() => filteredProjects.value.slice((page.value - 1) * 50, page.value * 50))
const label = status => ({ queued: '排队中', running: '正在发现', succeeded: '发现完成', failed: '发现失败', interrupted: '已中断' }[status] || status)
const inventoryLabel = status => ({ queued: '排队中', running: '盘点中', succeeded: '元数据盘点完成', failed: '盘点失败', interrupted: '已中断' }[status] || status)
const tone = status => ({ succeeded: 'success', failed: 'danger', interrupted: 'warning' }[status] || 'info')
watch(search, () => { page.value = 1 })

const { refresh, error: pollError } = useOperationPolling(async config => {
  const sourceId = selectedId.value
  const discoveryId = selectedDiscovery.value
  const inventoryId = selectedInventory.value
  const [sourceReply, healthReply] = await Promise.all([api.get('/git-migrations/sources', config), api.get('/git-migrations/status', config)])
  const id = sourceId ?? sourceReply.data[0]?.id
  let items = [], detail = null, inventories = [], inventoryDetail = null
  if (id) {
    inventories = (await api.get(`/git-migrations/sources/${id}/inventories`, config)).data
    const chosenInventory = inventories.find(row => row.id === inventoryId)?.id ?? inventories[0]?.id
    if (chosenInventory) inventoryDetail = (await api.get(`/git-migrations/inventories/${chosenInventory}`, config)).data
    items = (await api.get(`/git-migrations/sources/${id}/discoveries`, config)).data
    const chosen = items.find(row => row.id === discoveryId)?.id ?? items[0]?.id
    if (chosen) detail = (await api.get(`/git-migrations/discoveries/${chosen}`, config)).data
  }
  return { sourceId, discoveryId, inventoryId, inventories, inventoryDetail, id, sources: sourceReply.data, health: healthReply.data, items, detail }
}, result => {
  sources.value = result.sources
  health.value = result.health
  loaded.value = true
  if (result.sourceId !== selectedId.value || result.discoveryId !== selectedDiscovery.value || result.inventoryId !== selectedInventory.value) return
  selectedId.value = result.id ?? null
  inventoryHistory.value = result.inventories
  inventoryRecord.value = result.inventoryDetail
  selectedInventory.value = result.inventoryDetail?.id ?? null
  history.value = result.items
  record.value = result.detail
  selectedDiscovery.value = result.detail?.id ?? null
}, () => running.value)

function selectSource(id) {
  selectedId.value = id
  selectedDiscovery.value = null
  selectedInventory.value = null
  inventoryRecord.value = null
  inventoryHistory.value = []
  history.value = []
  record.value = null
  search.value = ''
  page.value = 1
  refresh()
}
function editSource(row) {
  editingId.value = row?.id ?? null
  Object.assign(form, { name: '', kind: 'gitlab', url: '', username: '', auth_method: 'password', secret: '', ca_pem: '' }, row || {})
  form.secret = ''
  dialog.value = true
}
async function save() {
  if (!form.name.trim() || !form.url.trim() || (form.auth_method === 'password' && !form.username.trim()) || (!editingId.value && !form.secret)) {
    ElMessage.error('请填写来源名称、连接地址及所需认证信息')
    return
  }
  saving.value = true
  const body = Object.fromEntries(['name', 'kind', 'url', 'username', 'auth_method', 'secret', 'ca_pem'].map(key => [key, form[key]]))
  if (editingId.value) body.version = form.version
  try {
    const reply = editingId.value ? await api.put(`/git-migrations/sources/${editingId.value}`, body) : await api.post('/git-migrations/sources', body)
    dialog.value = false
    form.secret = ''
    selectSource(reply.data.id)
    ElMessage.success('来源已保存')
  } catch { /* 请求拦截器提供错误反馈，保留表单供修正。 */ }
  finally { saving.value = false }
}
async function discover() {
  if (!selected.value || saving.value || running.value) return
  saving.value = true
  const id = selected.value.id
  const bytes = crypto.getRandomValues(new Uint8Array(20))
  const requestKey = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
  try {
    const reply = await api.post(`/git-migrations/sources/${id}/discoveries`, { source_version: selected.value.version, request_key: requestKey })
    if (selectedId.value === id) {
      record.value = reply.data
      selectedDiscovery.value = reply.data.id
      history.value = [reply.data, ...history.value]
    }
    refresh()
  } catch { /* 不盲目重发；刷新列表先核对是否已形成发现记录。 */ refresh() }
  finally { saving.value = false }
}
async function inspectProject(project) {
  if (!selected.value || !record.value || running.value || saving.value) return
  const sourceId = selected.value.id
  saving.value = true
  const requestKey = Array.from(crypto.getRandomValues(new Uint8Array(20)), b => b.toString(16).padStart(2, '0')).join('')
  try {
    const reply = await api.post(`/git-migrations/sources/${sourceId}/inventories`, { discovery_id: record.value.id, project_ids: [project.id], request_key: requestKey, capture_code: captureCode.value })
    if (selectedId.value === sourceId) {
      selectedInventory.value = reply.data.id
      inventoryRecord.value = reply.data
      inventoryHistory.value = [reply.data, ...inventoryHistory.value]
    }
  } catch { /* 先读取任务历史，不自动重发写请求。 */ }
  finally { saving.value = false; refresh() }
}
</script>

<style scoped>
.git-migration-page { display: grid; gap: 18px; min-width: 0; }
.heading, .section-heading { display: flex; justify-content: space-between; align-items: center; gap: 14px; flex-wrap: wrap; }
h2, h3 { margin: 0; color: #203752; } p { margin: 7px 0; color: #63758a; line-height: 1.6; overflow-wrap: anywhere; }
.heading .el-button, .actions { display: inline-flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.workspace { display: grid; grid-template-columns: minmax(230px, 0.75fr) minmax(0, 2fr); gap: 18px; }
.surface { background: rgba(255, 255, 255, .82); border: 1px solid rgba(198, 211, 227, .65); border-radius: 16px; padding: 20px; min-width: 0; box-shadow: 0 8px 26px rgba(45, 73, 107, .06); }
.sources { display: flex; flex-direction: column; gap: 12px; align-self: start; }
.source-row { display: grid; gap: 6px; width: 100%; text-align: left; padding: 14px; border: 1px solid #dce5ee; border-radius: 10px; background: white; cursor: pointer; color: #2c4058; overflow-wrap: anywhere; }
.source-row span, .source-row small { color: #65768a; } .source-row.selected { background: #eef5ff; border-color: #689ddc; } .source-row:focus-visible { outline: 2px solid #1765be; outline-offset: 2px; }
.projects { display: grid; align-content: start; gap: 16px; } .history-line, .record-line { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; color: #5b6b7e; font-size: 13px; }
.history-line .el-select { width: min(300px, 100%); } .project-path { display: block; color: #65768a; margin-top: 5px; overflow-wrap: anywhere; }
.inventory-section { display: grid; gap: 12px; min-width: 0; }
.project-table { width: 100%; } .note { font-size: 13px; } .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; } .form-grid .el-select { width: 100%; }
.actions .el-button + .el-button { margin-left: 0; }
@media (max-width: 900px) { .workspace { grid-template-columns: minmax(0, 1fr); } .sources { max-height: 360px; overflow-y: auto; } }
@media (max-width: 520px) { .surface { padding: 14px; } .form-grid { grid-template-columns: minmax(0, 1fr); gap: 0; } .actions { width: 100%; } }
</style>
