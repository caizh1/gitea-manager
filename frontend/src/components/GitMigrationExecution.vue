<template>
  <section class="execution">
    <h3>迁移执行与交接</h3>
    <p>选择接收群组后，来源完整路径将保留在该群组下。资料未全部核验时，仓库保留在管理员私有隔离区。</p>
    <div v-if="inventory?.status === 'succeeded' && !inventory.stale" class="execution-form">
      <el-select v-model="serverId" placeholder="选择目标实例" aria-label="目标实例" @change="resetGroups">
        <el-option v-for="server in servers" :key="server.id" :value="server.id" :label="server.name" />
      </el-select>
      <div v-if="serverId" class="group-browser">
        <div class="actions"><el-button :disabled="!parents.length" @click="goBack">上一级</el-button><span>{{ parents.at(-1)?.full_path || '根目录' }}</span></div>
        <div v-for="group in groups" :key="group.id" class="group-row">
          <el-radio v-model="groupId" :value="group.id" :disabled="!group.selectable" @change="chosen = group">{{ group.full_path }}</el-radio>
          <el-button link @click="enterGroup(group)">查看子群组</el-button>
        </div>
        <el-empty v-if="!groups.length" description="当前层级没有群组" :image-size="40" />
        <el-button v-if="nextGroup" @click="loadGroups(true)">加载更多群组</el-button>
      </div>
      <div v-if="chosen" class="paths"><strong>目标路径</strong><div v-for="item in inventory.result.projects" :key="item.project.id">{{ chosen.full_path }}/{{ item.project.path }}</div></div>
      <div v-if="principals.length" class="principal-map">
        <strong>人员与规则主体映射</strong>
        <p>填写目标已有账号。单个人员对应一个账号，团队或规则角色可用逗号分隔多个账号。目标权限需提前配置。</p>
        <p v-if="principals.includes('gitlab:ci:issuer')">强制 CI 需指定专用的可信回执账号。目标只接受该账号对当前提交发出的流水线成功状态，历史成功记录不会解除门禁。</p>
        <el-form label-position="top">
          <el-form-item v-for="key in principals" :key="key" :label="principalLabel(key)">
            <el-input v-model="principalInputs[key]" placeholder="目标用户登录名" />
          </el-form-item>
        </el-form>
      </div>
      <el-checkbox v-model="frozen">已确认所列项目及依赖均已停止写入，并授权按当前盘点内容执行迁移</el-checkbox>
      <el-button type="primary" :disabled="!serverId || !groupId || !frozen || activeRun || principals.some(key => !principalInputs[key]?.trim())" :loading="busy" @click="start">执行迁移</el-button>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-table :data="runs" empty-text="尚未执行迁移">
      <el-table-column prop="id" label="编号" width="75" />
      <el-table-column label="状态" min-width="120"><template #default="{ row }">{{ statusLabel(row.status) }}</template></el-table-column>
      <el-table-column prop="stage" label="当前步骤" min-width="200" />
      <el-table-column label="操作" min-width="240"><template #default="{ row }"><div class="actions"><el-button link @click="openDetail(row)">详情</el-button><template v-if="['interrupted', 'uncertain', 'quarantined'].includes(row.status)"><el-button link :disabled="busy" @click="resume(row)">继续核对</el-button><el-button link type="danger" :disabled="busy" @click="abandon(row)">终止并保留现场</el-button></template></div></template></el-table-column>
    </el-table>
    <el-dialog v-model="showDetail" title="迁移执行详情" width="min(800px, 96vw)">
      <template v-if="detail">
        <p>任务 #{{ detail.id }} · {{ statusLabel(detail.status) }}</p>
        <el-alert v-if="detail.error" :title="detail.error" type="error" :closable="false" />
        <div v-if="['interrupted', 'uncertain', 'quarantined'].includes(detail.status)" class="actions"><el-button @click="rotateTarget(detail)">更新目标令牌</el-button><span>需属于原目标管理员，核对身份后可继续同一任务。</span></div>
        <div v-for="[id, project] in projectCheckpoints" :key="id" class="checkpoint">
          <strong>来源项目 #{{ id }} · {{ project.final_path || project.repo_name || '尚未创建目标' }}</strong>
          <p>独立来源核验：{{ project.source_verified ? '通过' : '未完成' }}；代码核验：{{ project.code_verified ? '通过' : '未完成' }}；原生资料核验：{{ project.objects_verified ? '通过' : '未完成' }}；交接：{{ project.handed_off ? '完成' : '未完成' }}</p>
          <el-table v-if="project.pending?.length || project.gaps?.length" :data="[...(project.pending || []), ...(project.gaps || [])]">
            <el-table-column prop="category" label="尚未完成的范围" /><el-table-column prop="reason" label="原因" />
          </el-table>
        </div>
      </template>
    </el-dialog>
  </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessageBox } from 'element-plus'
import { api } from '../api'
import { useOperationPolling } from '../composables/useOperationPolling'
const props = defineProps({ inventory: Object, sourceId: Number })
const servers = ref([]), serverId = ref(null), groupId = ref(null), groups = ref([]), parents = ref([]), chosen = ref(null), nextGroup = ref(0)
const frozen = ref(false), busy = ref(false), runs = ref([]), detail = ref(null)
const principals = ref([]), principalInputs = ref({})
const principalDetails = ref({})
const principalLabel = key => key === 'gitlab:ci:issuer' ? '可信 CI 回执账号（目标已有专用账号）' : principalDetails.value[key] ? `${principalDetails.value[key].username} · ${principalDetails.value[key].roles.join('、')} · 来源编号 ${key.split(':').at(-1)}` : key.startsWith('gitlab:access:') ? `GitLab ${({ 30: '开发者', 40: '维护者', 50: '所有者' })[key.split(':').at(-1)] || '角色'}及更高权限` : `${key.startsWith('gitea:') ? 'Gitea' : 'GitLab'} ${key.includes(':group:') || key.includes(':team:') || key.includes(':review-team:') ? '群组或团队' : '用户编号'} ${key.split(':').at(-1)}`
let principalRequest = 0, groupRequest = 0
const statusLabel = status => ({ queued: '等待执行', running: '正在迁移', succeeded: '交接完成', interrupted: '已中断', uncertain: '待核对', quarantined: '隔离待处理', abandoned: '已终止，保留现场' }[status] || status)
const activeRun = computed(() => runs.value.some(row => ['queued', 'running'].includes(row.status)))
const showDetail = computed({ get: () => !!detail.value, set: value => { if (!value) detail.value = null } })
const projectCheckpoints = computed(() => Object.entries(detail.value?.checkpoint || {}).filter(([id]) => /^\d+$/.test(id)))
const { refresh, error } = useOperationPolling(async config => {
  const rows = (await api.get('/git-migrations/runs', config)).data
  const selectedId = detail.value?.id
  const selected = selectedId ? (await api.get(`/git-migrations/runs/${selectedId}`, config)).data : null
  return { rows, selected }
}, ({ rows, selected }) => {
  runs.value = rows.filter(row => row.source_id === props.sourceId)
  if (detail.value && selected?.id === detail.value.id) detail.value = selected
}, () => activeRun.value)
function openDetail(row) { detail.value = row; refresh() }
onMounted(async () => { servers.value = (await api.get('/servers')).data.filter(row => row.role !== 'archive') })
watch(() => props.sourceId, () => { frozen.value = false; refresh() })
watch(() => props.inventory?.id, async id => {
  frozen.value = false; principals.value = []; principalInputs.value = {}; principalDetails.value = {}
  const request = ++principalRequest
  if (!id || props.inventory?.status !== 'succeeded') return
  const [result, details] = await Promise.all([api.get(`/git-migrations/inventories/${id}/principals`), api.get(`/git-migrations/inventories/${id}/principal-details`)])
  if (request === principalRequest) { principals.value = result.data; principalDetails.value = Object.fromEntries(details.data.map(row => [row.key, row])) }
}, { immediate: true })
async function loadGroups(append = false) {
  const request = ++groupRequest
  const reply = await api.get(`/git-migrations/targets/${serverId.value}/groups`, { params: { parent_id: parents.value.at(-1)?.id || 0, after_id: append ? nextGroup.value : 0 } })
  if (request !== groupRequest) return
  groups.value = append ? [...groups.value, ...reply.data.groups] : reply.data.groups
  nextGroup.value = reply.data.next_id
}
function resetGroups() { groupId.value = null; chosen.value = null; parents.value = []; principalInputs.value = {}; frozen.value = false; loadGroups() }
function enterGroup(group) { parents.value.push(group); loadGroups() }
function goBack() { parents.value.pop(); loadGroups() }
async function start() {
  busy.value = true
  try {
    const requestKey = Array.from(crypto.getRandomValues(new Uint8Array(20)), value => value.toString(16).padStart(2, '0')).join('')
    const principalMap = Object.fromEntries(principals.value.map(key => [key, principalInputs.value[key].split(/[,，]/).map(value => value.trim()).filter(Boolean)]))
    const reply = await api.post('/git-migrations/runs', { inventory_id: props.inventory.id, server_id: serverId.value, group_id: groupId.value, source_frozen: frozen.value, request_key: requestKey, principal_map: principalMap })
    detail.value = reply.data
  } finally { busy.value = false; refresh() }
}
async function resume(row) {
  busy.value = true
  try { await api.post(`/git-migrations/runs/${row.id}/resume`, {}) }
  finally { busy.value = false; refresh() }
}
async function abandon(row) {
  let reason
  try { reason = (await ElMessageBox.prompt('未完成的仓库将归档保留，接收会话会关闭。请填写终止原因。', '终止迁移', { inputValidator: value => !!value?.trim() || '请填写终止原因', confirmButtonText: '终止并保留现场' })).value }
  catch { return }
  busy.value = true
  try { await api.post(`/git-migrations/runs/${row.id}/abandon`, { reason }) }
  finally { busy.value = false; refresh() }
}
async function rotateTarget(row) {
  let token
  try { token = (await ElMessageBox.prompt('填写原目标管理员的新令牌，实例地址和授权范围保持固定。', '更新目标令牌', { inputType: 'password', inputValidator: value => !!value?.trim() || '请填写新令牌' })).value }
  catch { return }
  busy.value = true
  try { await api.post(`/git-migrations/runs/${row.id}/target-credentials`, { token }) }
  finally { token = ''; busy.value = false; refresh() }
}
</script>

<style scoped>
.execution, .execution-form, .group-browser, .paths, .checkpoint { display: grid; gap: 12px; min-width: 0; }
.execution { border-top: 1px solid #dce5ee; padding-top: 18px; }
.execution-form { max-width: 720px; }
.group-row, .actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; min-width: 0; }
.group-row { justify-content: space-between; }
.group-row :deep(.el-radio__label), .paths { white-space: normal; overflow-wrap: anywhere; }
.group-row :deep(.el-radio) { height: auto; min-height: 32px; max-width: 100%; }
p { color: #63758a; margin: 0; line-height: 1.6; }
</style>
