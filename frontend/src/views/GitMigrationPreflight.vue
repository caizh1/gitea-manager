<template>
  <main class="preflight-page" v-loading="!preflight">
    <header><div><h2>预迁移与完整性检查</h2><p>预迁移 #{{ preflight?.id }} · {{ preflight?.target?.path }}</p></div><el-button @click="router.push('/git-migrations')">返回迁移中心</el-button></header>
    <el-alert title="预迁移在管理员私有隔离区实际导入并回读资料，准备最终归属、规则与制品，整批完成前禁止业务访问。检查通过表示当前快照具备迁移条件，不是对未来故障的概率承诺。" type="info" :closable="false" show-icon />
    <el-alert v-if="error || preflight?.error || preflight?.stale" :title="error || preflight?.error || preflight?.stale" type="warning" :closable="false" show-icon />
    <section class="gates">
      <div><h3>可执行性检查</h3><el-tag :type="readiness === '通过' ? 'success' : readiness === '阻断' ? 'danger' : 'info'">{{ readiness }}</el-tag><p>已通过 {{ preflight?.checks_passed || 0 }} / {{ preflight?.checks_total || 0 }} 项检查</p></div>
      <div><h3>隔离预迁移</h3><el-tag :type="preflight?.result?.rehearsal === 'passed' ? 'success' : 'info'">{{ preflight?.result?.rehearsal === 'passed' ? '通过' : preflight?.result?.rehearsal === 'blocked' ? '未通过' : '尚未完成' }}</el-tag><p>{{ preflight?.stage || '等待执行' }}</p></div>
    </section>
    <section v-if="preflight?.compatibility?.differences?.length" class="report">
      <h3>来源与目标的讨论规则差异</h3>
      <p>讨论状态会作为只读来源历史完整保留。目标采用 Gitea 原生合并规则，不执行来源的“全部讨论必须解决”限制。</p>
      <el-table :data="preflight.compatibility.differences">
        <el-table-column prop="path" label="来源项目" min-width="240" />
        <el-table-column prop="threads" label="讨论线程" width="110" />
        <el-table-column prop="unresolved" label="待解决线程" width="120" />
      </el-table>
      <p v-if="preflight.compatibility.acknowledged">已确认本批次行为差异；数据完整性仍须通过全部核验。</p>
      <template v-else>
        <el-checkbox v-model="acceptDifference">我已核对以上全部项目，同意目标采用 Gitea 原生合并规则</el-checkbox>
        <div class="actions"><el-button type="primary" :disabled="!acceptDifference || busy || preflight.status !== 'blocked'" @click="acknowledge">确认差异并重新预检</el-button></div>
      </template>
    </section>
    <section v-if="preflight?.capacity" class="report"><h3>运行中容量监测</h3><p>Manager 剩余 {{ gib(preflight.capacity.manager.free_bytes) }} GiB · 目标最小剩余 {{ targetFree }} GiB · 每 5 秒检查，容量不足时停止写入并保留检查点。</p></section>
    <section class="report"><h3>检查结果与处理建议</h3><el-table :data="preflight?.result?.checks || []" empty-text="检查开始后会逐项显示结果">
      <el-table-column prop="title" label="检查项目" min-width="180" />
      <el-table-column prop="project" label="来源范围" min-width="140" />
      <el-table-column label="结果" width="85"><template #default="{ row }"><el-tag :type="row.status === 'passed' ? 'success' : 'danger'">{{ row.status === 'passed' ? '通过' : '阻断' }}</el-tag></template></el-table-column>
      <el-table-column label="证据或失败原因" min-width="260"><template #default="{ row }">{{ detail(row.detail) }}</template></el-table-column>
      <el-table-column prop="remedy" label="处理建议" min-width="240" />
    </el-table></section>
    <section v-if="preflight?.run_id" class="launch"><h3>正式迁移 #{{ preflight.run_id }}</h3><p>{{ run?.stage || '已创建，等待执行' }}</p><el-alert v-if="run?.error" :title="run.error" type="error" show-icon :closable="false" /><el-tag :type="run?.status === 'succeeded' ? 'success' : 'info'">{{ run?.status === 'succeeded' ? '迁移及最终交接完成' : '请在迁移中心继续查看执行结果' }}</el-tag></section>
    <footer v-else>
      <p v-if="preflight?.expires_at">本次结果有效至 {{ localTime(preflight.expires_at) }}。人员、来源、目标变化后需要重新预迁移。</p>
      <el-checkbox v-model="frozen" :disabled="!canStart">已确认所有来源项目及依赖均已停止写入</el-checkbox>
      <div class="actions"><el-button v-if="['blocked','interrupted'].includes(preflight?.status) && !preflight?.stale && preflight?.phase !== 'cancel'" :disabled="busy" @click="resume">从检查点继续</el-button><el-button v-if="['ready','blocked','interrupted'].includes(preflight?.status)" :disabled="busy" @click="rotateTarget">更新目标令牌</el-button><el-button v-if="['ready','blocked','interrupted'].includes(preflight?.status)" :disabled="busy" @click="cancel">{{ preflight?.phase === 'cancel' ? '重试结束预迁移' : '结束预迁移并保留现场' }}</el-button><el-button v-if="preflight?.status === 'cancelled'" @click="backToPreparation">返回准备并重新预迁移</el-button><el-button type="primary" :disabled="!canStart || !frozen || busy" :loading="busy" @click="start">复检通过后创建正式迁移任务</el-button></div>
      <p v-if="!canStart">所有检查和预迁移均通过后，才允许正式启动；此处不能跳过阻断项。</p>
    </footer>
  </main>
</template>
<script setup>
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { api } from '../api'
import { useOperationPolling } from '../composables/useOperationPolling'
const route = useRoute(), router = useRouter(), preflight = ref(null), run = ref(null), frozen = ref(false), busy = ref(false), now = ref(Date.now())
const clock = setInterval(() => { now.value = Date.now() }, 1000)
const acceptDifference = ref(false)
onUnmounted(() => clearInterval(clock))
const canStart = computed(() => preflight.value?.can_start && (!preflight.value.expires_at || Date.parse(preflight.value.expires_at) > now.value))
const readiness = computed(() => {
  const checks = preflight.value?.result?.checks || []
  if (checks.some(row => row.status === 'blocked')) return '阻断'
  return checks.length && ['ready','consumed'].includes(preflight.value?.status) && !preflight.value?.stale ? '通过' : '检查中'
})
const { refresh, error } = useOperationPolling(async config => {
  const id = route.params.preflightId
  const value = (await api.get(`/git-migrations/preflights/${id}`, config)).data
  const execution = value.run_id ? (await api.get(`/git-migrations/runs/${value.run_id}`, config)).data : null
  return { id, value, execution }
}, result => {
  if (String(result.id) !== String(route.params.preflightId)) return
  preflight.value = result.value; run.value = result.execution
}, () => ['queued','running'].includes(preflight.value?.status) || ['queued','running'].includes(run.value?.status))
watch(() => route.params.preflightId, () => { frozen.value = false; refresh() })
watch(() => preflight.value?.compatibility?.digest, () => { acceptDifference.value = false })
const gib = value => (Number(value) / 1024 ** 3).toFixed(2)
const targetFree = computed(() => gib(Math.min(...Object.values(preflight.value?.capacity?.target?.storage || {}).map(value => value.free_bytes))))
const requestKey = crypto.randomUUID().replaceAll('-', '')
async function acknowledge() {
  busy.value = true
  try {
    await api.post(`/git-migrations/preflights/${preflight.value.id}/compatibility-acknowledgements`, { digest: preflight.value.compatibility.digest })
    await refresh()
  } finally { busy.value = false }
}
async function start() {
  busy.value = true
  try { await api.post(`/git-migrations/preflights/${preflight.value.id}/start`, { source_frozen: frozen.value, request_key: requestKey }); await refresh() }
  finally { busy.value = false }
}
async function resume() {
  busy.value = true
  try { await api.post(`/git-migrations/preflights/${preflight.value.id}/resume`, {}); await refresh() }
  finally { busy.value = false }
}
async function rotateTarget() {
  let token
  try { token = (await ElMessageBox.prompt('填写原目标管理员的新令牌，以便继续核对或关闭隔离现场。', '更新目标令牌', { inputType: 'password', inputValidator: value => !!value?.trim() || '请填写令牌' })).value } catch { return }
  busy.value = true
  try { await api.post(`/git-migrations/preflights/${preflight.value.id}/target-credentials`, { token }); await refresh() }
  finally { token = ''; busy.value = false }
}
async function cancel() {
  try { await ElMessageBox.confirm('将关闭本次预迁移接收会话，隔离副本保留归档。后续需要建立新的预迁移记录。', '结束预迁移', { confirmButtonText: '结束并保留现场', cancelButtonText: '返回' }) } catch { return }
  busy.value = true
  try { await api.post(`/git-migrations/preflights/${preflight.value.id}/cancel`, {}); await refresh() }
  finally { busy.value = false }
}
function backToPreparation() { router.push(preflight.value.mapping_id ? `/git-migrations/account-mappings/${preflight.value.mapping_id}` : '/git-migrations') }
function localTime(value) { return new Date(value).toLocaleString('zh-CN', { hour12: false }) }
function detail(value) {
  if (typeof value !== 'object' || value === null) return String(value ?? '')
  if (value.budget) return `Manager 预留 ${(value.budget.manager_required / 1024 ** 3).toFixed(2)} GiB；目标预留 ${(value.budget.target_required / 1024 ** 3).toFixed(2)} GiB；inode 与实际写入探测通过`
  if (value.path) return `群组 ${value.path}，身份与权限快照一致`
  if (value.paths) return value.paths.join('；')
  if ('git' in value) return `代码${value.git ? '通过' : '未完成'}；原生资料${value.objects ? '通过' : '未完成'}；附件 ${value.assets || 0} 项`
  if ('source_sha256' in value) return `独立清单已核对：对象 ${value.objects} 项、文件 ${value.files} 项、规则 ${value.rules} 项`
  if ('refs' in value) return `引用 ${value.refs} 项、LFS 对象 ${value.lfs} 项`
  if ('objects' in value) return `来源对象 ${value.objects} 项、文件 ${value.files} 项`
  return '已核对'
}
</script>
<style scoped>
.preflight-page { display: grid; gap: 18px; min-width: 0; } header,.actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; } h2 { font-size: 28px; margin: 0 0 8px; } h3 { font-size: 17px; margin: 0 0 14px; } p { color: #687b91; line-height: 1.7; font-size: 13px; margin: 8px 0 0; }
.gates { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }.gates > div,.report,.launch,footer { background: rgba(255,255,255,.92); border: 1px solid #dce7f2; border-radius: 10px; padding: 18px; min-width: 0; }.actions { justify-content: flex-end; margin-top: 16px; }footer :deep(.el-checkbox) { margin-top: 14px; height: auto; white-space: normal; }footer :deep(.el-checkbox__label) { white-space: normal; }
@media (max-width: 760px) { .gates { grid-template-columns: 1fr; }.actions :deep(.el-button) { white-space: normal; height: auto; min-height: 32px; }.preflight-page { gap: 12px; } }
</style>
