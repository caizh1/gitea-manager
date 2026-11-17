<template>
  <div class="management-page">
    <div class="setup-heading">
      <h2>服务器接入与诊断</h2>
      <el-radio-group v-model="form.mode" :disabled="busy" aria-label="接入方式">
        <el-radio-button value="local">本机 Gitea</el-radio-button>
        <el-radio-button value="ssh">跨主机 Gitea</el-radio-button>
      </el-radio-group>
    </div>

    <section class="glass-card connection-card" aria-label="连接信息">
      <el-form label-width="150px" :disabled="busy" class="connection-form">
        <template v-if="form.mode === 'ssh'">
          <el-form-item label="目标主机" required>
            <el-input v-model="form.host" placeholder="Gitea 宿主机 IP 或域名" />
          </el-form-item>
          <el-form-item label="系统 SSH 端口" required>
            <el-input-number v-model="form.ssh_port" :min="1" :max="65535" />
          </el-form-item>
          <el-form-item label="管理账号" required>
            <el-input v-model="form.ssh_user" placeholder="root" />
          </el-form-item>
          <details class="public-key-details">
            <summary>查看管理公钥</summary>
            <p>请目标主机管理员将此公钥追加到管理账号的 ~/.ssh/authorized_keys，目录权限设为 700，文件权限设为 600。该账号须能直接执行 Docker 命令并操作备份文件。使用系统 SSH 端口。</p>
            <el-input :model-value="publicKey" type="textarea" readonly :rows="4" aria-label="管理公钥" />
            <div class="actions">
              <el-button :disabled="!publicKey || busy" @click="copyPublicKey">复制公钥</el-button>
              <el-button :disabled="!publicKey || busy" @click="downloadPublicKey">下载公钥</el-button>
            </div>
          </details>
          <el-form-item label="主机指纹">
            <div class="actions">
              <el-button :disabled="!sshReady" :loading="activeAction === 'trust'" @click="trustHost">
                <ShieldCheck :size="17" aria-hidden="true" />核对并信任主机
              </el-button>
              <span v-if="trustedHost" class="state-text success" role="status"><CircleCheck :size="17" aria-hidden="true" />主机指纹已确认</span>
            </div>
          </el-form-item>
        </template>
        <el-form-item label="服务器名称">
          <el-input v-model="form.name" placeholder="可选，默认使用实例名称" />
        </el-form-item>
        <el-form-item label="Gitea 访问地址">
          <div class="field-content">
            <el-input v-model="form.gitea_url" placeholder="例如 http://192.0.2.10:3000，可留空读取实例配置" />
            <span class="field-help">填写带 http:// 或 https:// 的 Web 根地址，包含实际端口和部署子路径。</span>
          </div>
        </el-form-item>
        <el-form-item label="管理员 API Token" :required="form.mode === 'ssh'">
          <div class="field-content">
            <el-input v-model="form.api_token" type="password" show-password autocomplete="off"
              :placeholder="form.mode === 'ssh' ? '粘贴目标 Gitea 已有管理员的 API Token' : '可选，粘贴已有管理员的 API Token'" />
            <span class="field-help">{{ form.mode === 'ssh' ? '使用目标 Gitea 管理员在个人设置 → 应用中生成的令牌；接入前会校验管理员身份和管理 API 权限。' : '留空时，为实例中唯一启用的管理员自动生成令牌；有多个管理员时请手动填写。' }}</span>
          </div>
        </el-form-item>
      </el-form>
    </section>

    <section class="glass-card instance-section" aria-labelledby="instance-heading">
      <div class="section-heading">
        <h3 id="instance-heading">选择 Gitea 实例</h3>
        <el-button :disabled="busy || !sshReady" :loading="activeAction === 'discover'" @click="discover">
          <Search :size="17" aria-hidden="true" />{{ discoveryComplete ? '重新发现实例' : '发现并检查实例' }}
        </el-button>
      </div>
      <p class="discovery-status" role="status" aria-live="polite">
        <CircleCheck v-if="selectedInstance" :size="17" class="success" aria-hidden="true" />
        <Info v-else :size="17" aria-hidden="true" />
        <span>{{ discoveryHint }}</span>
      </p>
      <div v-if="instances.length" class="instance-list" role="group" aria-label="可选 Gitea 实例">
        <button v-for="item in instances" :key="item.id" type="button" class="instance-choice"
          :class="{ 'is-selected': selected === item.id }" :aria-pressed="selected === item.id"
          :disabled="busy || !item.running" @click="selected = item.id">
          <span class="instance-content">
            <strong>{{ item.name }}</strong>
            <span class="instance-meta">
              <span>项目 {{ item.project }}</span><span>{{ item.port ? `HTTP 端口 ${item.port}` : 'HTTP 端口未公开' }}</span><span>数据库 {{ item.pg_container }}</span>
            </span>
          </span>
          <span class="instance-states">
            <span class="state-text" :class="item.running ? 'success' : 'muted'"><Server :size="16" aria-hidden="true" />{{ item.running ? '运行中' : '未运行' }}</span>
            <span class="state-text selection-state"><CircleCheck v-if="selected === item.id" :size="20" aria-hidden="true" />{{ selected === item.id ? '已选中' : item.running ? '点击选择' : '请先启动' }}</span>
          </span>
        </button>
      </div>
      <div class="role-section">
        <h3 id="server-role-label">服务器角色 <span class="required-mark">*</span></h3>
        <div class="role-options" role="group" aria-labelledby="server-role-label">
          <button type="button" class="role-choice" :class="{ 'is-selected': form.role === 'primary' }"
            :aria-pressed="form.role === 'primary'" :disabled="busy" @click="form.role = 'primary'">
            <DatabaseBackup :size="22" aria-hidden="true" /><span><strong>主服务器</strong><small>可作为备份来源</small></span>
            <CircleCheck v-if="form.role === 'primary'" :size="20" class="role-check" aria-hidden="true" />
          </button>
          <button type="button" class="role-choice" :class="{ 'is-selected': form.role === 'backup' }"
            :aria-pressed="form.role === 'backup'" :disabled="busy" @click="form.role = 'backup'">
            <ArchiveRestore :size="22" aria-hidden="true" /><span><strong>备服务器</strong><small>可作为恢复目标</small></span>
            <CircleCheck v-if="form.role === 'backup'" :size="20" class="role-check" aria-hidden="true" />
          </button>
        </div>
        <p class="field-help">角色用于管理分类，接入后可在服务器管理中修改。</p>
      </div>
    </section>

    <div class="glass-card connect-footer">
      <div class="connect-summary" aria-live="polite"><strong>{{ connected ? '服务器已添加' : selectedInstance ? `将连接 ${selectedInstance.name}` : '等待选择实例' }}</strong><span>{{ connectionHint }}</span></div>
      <el-button v-if="connected" type="primary" @click="$router.push('/servers')">查看服务器</el-button>
      <el-button v-else type="primary" class="connect-button" :disabled="!canConnect" :loading="activeAction === 'connect'" @click="connect">连接并添加服务器</el-button>
    </div>
    <el-alert v-if="message" :title="message" :type="failed ? 'error' : 'success'" :closable="false" role="status" />
    <a v-if="lastDiagnostic" :href="`/api/management/diagnostics/operation/${lastDiagnostic}`">下载本次操作诊断</a>

    <section class="glass-card diagnostics-card">
      <div class="section-heading"><h3>安装与运行诊断</h3><el-button @click="loadReports">刷新</el-button></div>
      <el-alert :type="runtime.ready ? 'success' : 'warning'" :closable="false"
        :title="runtime.ready ? `AI 运行时就绪${runtime.version ? `：${runtime.version}` : ''}（${runtime.mode === 'legacy' ? '兼容模式' : '标准模式'}）` : 'AI 暂不可用，核心管理与备份可以继续使用'"
        :description="runtime.reason" />
      <el-table :data="reports">
        <el-table-column prop="标题" label="操作" />
        <el-table-column prop="状态" label="结果" />
        <el-table-column prop="id" label="操作编号" />
        <el-table-column label="诊断"><template #default="{ row }"><a :href="`/api/management/diagnostics/${row.kind}/${row.id}`">下载诊断包</a> <el-button link @click="showSteps(row)">步骤详情</el-button></template></el-table-column>
      </el-table>
    </section>
    <el-dialog append-to-body v-model="stepsVisible" title="操作步骤" width="90%"><el-table :data="steps"><el-table-column prop="时间" label="时间" /><el-table-column prop="步骤" label="步骤" /><el-table-column prop="状态" label="状态" /><el-table-column prop="说明" label="详情" /></el-table></el-dialog>
  </div>
</template>

<script setup>
import { computed, ref, onMounted, onUnmounted, watch } from 'vue'
import { ElMessageBox, ElMessage } from 'element-plus'
import { ArchiveRestore, CircleCheck, DatabaseBackup, Info, Search, Server, ShieldCheck } from '@lucide/vue'
import { api } from '../api'

const form = ref({ mode: 'local', host: '', ssh_port: 22, ssh_user: 'root', name: '', gitea_url: '', api_token: '', role: '' })
const runtime = ref({ ready: false }), publicKey = ref(''), csrf = ref('')
const instances = ref([]), selected = ref(''), discoveryComplete = ref(false), autoSelected = ref(false)
const activeAction = ref(''), message = ref(''), failed = ref(false), lastDiagnostic = ref(''), reports = ref([])
const trustedHost = ref(false), connected = ref(false), stepsVisible = ref(false), steps = ref([])
const busy = computed(() => Boolean(activeAction.value))
const sshReady = computed(() => form.value.mode === 'local' || Boolean(form.value.host.trim() && form.value.ssh_user.trim() && form.value.ssh_port))
const selectedInstance = computed(() => instances.value.find(item => item.id === selected.value && item.running))
const canConnect = computed(() => !busy.value && Boolean(selectedInstance.value && form.value.role && (form.value.mode === 'local' || form.value.api_token.trim())))
const discoveryHint = computed(() => {
  if (activeAction.value === 'discover') return '正在通过管理连接检查实例…'
  if (!discoveryComplete.value) return '发现实例后，在下方选择要接入的 Gitea。'
  if (!instances.value.length) return '未发现 Gitea 与 PostgreSQL 关联实例，请检查目标主机的容器配置。'
  if (!instances.value.some(item => item.running)) return '已发现的实例均未运行，请先启动 Gitea 后重新发现。'
  if (autoSelected.value && selectedInstance.value) return `已发现 ${instances.value.length} 个实例，已自动选中唯一运行中的实例。`
  return selectedInstance.value ? `已选中 ${selectedInstance.value.name}。` : `已发现 ${instances.value.length} 个实例，请点击下方卡片选择。`
})
const connectionHint = computed(() => {
  if (connected.value) return '可前往服务器管理查看连接状态。'
  if (!selectedInstance.value) return '先发现并选择一个运行中的实例。'
  if (form.value.mode === 'ssh' && !form.value.api_token.trim()) return '请填写已有 Gitea 管理员的 API Token。'
  if (!form.value.role) return '请选择主服务器或备服务器。'
  return form.value.role === 'primary' ? '主服务器 · 可作为备份来源' : '备服务器 · 可作为恢复目标'
})

watch(() => [form.value.mode, form.value.host, form.value.ssh_port, form.value.ssh_user], () => {
  instances.value = []; selected.value = ''; discoveryComplete.value = false; autoSelected.value = false
  trustedHost.value = false; connected.value = false; form.value.api_token = ''; message.value = ''; lastDiagnostic.value = ''
})
watch(() => [form.value.name, form.value.gitea_url, form.value.role, selected.value], () => { connected.value = false })

let runtimeTimer
onUnmounted(() => clearInterval(runtimeTimer))
const post = (path, body) => api.post('/management/' + path, body, { headers: { 'X-Management-CSRF': csrf.value }, notifyError: false })
const target = () => ({ mode: form.value.mode, host: form.value.host.trim(), ssh_port: form.value.ssh_port, ssh_user: form.value.ssh_user.trim() })

async function loadReports() {
  try { reports.value = (await api.get('/management/diagnostics', { notifyError: false })).data.reports }
  catch { ElMessage.warning('诊断列表暂时无法刷新') }
}
async function run(action, callback) {
  if (busy.value) return
  activeAction.value = action; message.value = ''; lastDiagnostic.value = ''; failed.value = false
  try { await callback() }
  catch (error) {
    if (error === 'cancel' || error === 'close') return
    failed.value = true; message.value = error.response?.data?.error || error.message
    lastDiagnostic.value = error.response?.data?.diagnostic_id || ''
  } finally { activeAction.value = ''; await loadReports() }
}
function discover() {
  if (!sshReady.value) return
  return run('discover', async () => {
    instances.value = []; selected.value = ''; discoveryComplete.value = false; autoSelected.value = false
    const { data } = await post('discover', target())
    instances.value = data.instances; discoveryComplete.value = true; lastDiagnostic.value = data.diagnostic_id
    const running = data.instances.filter(item => item.running)
    if (running.length === 1) { selected.value = running[0].id; autoSelected.value = true }
  })
}
function connect() {
  if (!canConnect.value) return
  return run('connect', async () => {
    const payload = { ...target(), name: form.value.name.trim(), gitea_url: form.value.gitea_url.trim(), api_token: form.value.api_token.trim(), role: form.value.role, instance_id: selected.value }
    const { data } = await post('connect', payload)
    connected.value = true; form.value.api_token = ''; lastDiagnostic.value = data.diagnostic_id
    message.value = payload.role === 'primary' ? '主服务器已连接，可在备份管理中选择' : '备服务器已连接，可在恢复操作中选择'
  })
}
function trustHost() {
  if (!sshReady.value) return
  return run('trust', async () => {
    trustedHost.value = false
    const { data } = await post('ssh-fingerprint', target())
    await ElMessageBox.confirm(`请与目标管理员核对主机指纹：${data.fingerprint}`, '确认主机身份', { confirmButtonText: '确认并信任', cancelButtonText: '取消' })
    await post('ssh-trust', { fingerprint: data.fingerprint })
    trustedHost.value = true
  })
}
async function copyPublicKey() {
  try { await navigator.clipboard.writeText(publicKey.value); ElMessage.success('公钥已复制') }
  catch { ElMessage.info('当前浏览器不支持自动复制，请选择公钥文本复制') }
}
function downloadPublicKey() {
  const url = URL.createObjectURL(new Blob([publicKey.value + '\n'], { type: 'text/plain' }))
  const link = document.createElement('a'); link.href = url; link.download = 'gitea-manager.pub'; link.click(); URL.revokeObjectURL(url)
}
async function showSteps(row) {
  steps.value = (await api.get(`/management/diagnostics/${row.kind}/${row.id}/steps`)).data.events
  stepsVisible.value = true
}
onMounted(async () => {
  try {
    const { data } = await api.get('/management/status')
    runtime.value = data.runtime; csrf.value = data.csrf
  } catch (error) { failed.value = true; message.value = error.response?.data?.error || error.message; return }
  runtimeTimer = setInterval(async () => {
    try { runtime.value = (await api.get('/management/status', { notifyError: false })).data.runtime }
    catch { runtime.value = { ready: false, reason: '暂时无法获取执行器状态，请检查服务连接' } }
  }, 10000)
  try { publicKey.value = (await api.get('/management/ssh-public-key', { notifyError: false })).data.public_key }
  catch (error) { failed.value = true; message.value = error.response?.data?.error || error.message }
  await loadReports()
  if (form.value.mode === 'local') await discover()
})
</script>

<style scoped>
.management-page { min-width: 0; display: flex; flex-direction: column; gap: 20px; }
.setup-heading, .section-heading { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 16px; }
.setup-heading { justify-content: flex-start; gap: 24px; }
h2, h3, p { margin: 0; }
h2 { font-size: 24px; } h3 { font-size: 18px; }
.glass-card { padding: 22px; display: flex; flex-direction: column; gap: 16px; }
.connection-form :deep(.el-form-item) { margin-bottom: 14px; }
.connection-form :deep(.el-form-item:last-child) { margin-bottom: 0; }
.connection-form :deep(.el-form-item__label) { align-items: center; }
.connection-form :deep(.el-form-item__content) { min-width: 0; }
.connection-form :deep(.el-input), .connection-form :deep(.el-input-number) { min-height: 38px; }
.field-content { width: 100%; min-width: 0; display: flex; flex-direction: column; gap: 6px; }
.field-help { color: var(--text-secondary); font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
.actions { display: flex; align-items: center; flex-wrap: wrap; gap: 10px 16px; min-width: 0; }
.actions :deep(.el-button + .el-button) { margin-left: 0; }
.public-key-details { border: 1px solid var(--glass-border-dark); border-radius: 10px; padding: 10px 16px; margin-bottom: 14px; }
.public-key-details summary { cursor: pointer; font-weight: 600; color: var(--text-secondary); }
.public-key-details p { margin: 12px 0; color: var(--text-secondary); line-height: 1.7; font-size: 13px; overflow-wrap: anywhere; }
.public-key-details .actions { margin-top: 10px; }
.state-text, .discovery-status { display: inline-flex; align-items: center; gap: 8px; }
.state-text { white-space: nowrap; font-size: 14px; }
.success { color: var(--color-success); } .muted { color: var(--text-muted); }
.discovery-status { color: var(--text-secondary); font-size: 14px; line-height: 1.6; }
.discovery-status svg, .state-text svg, .role-choice > svg { flex-shrink: 0; }
.instance-list { display: grid; gap: 12px; min-width: 0; }
.instance-choice, .role-choice { font: inherit; color: var(--text-primary); text-align: left; cursor: pointer; border: 1px solid #cedbea; background: rgba(255, 255, 255, .35); border-radius: 12px; transition: border-color .15s, background-color .15s; }
.instance-choice { display: flex; align-items: center; justify-content: space-between; gap: 20px; width: 100%; min-height: 98px; padding: 18px 24px; }
.instance-choice:hover:not(:disabled), .role-choice:hover:not(:disabled) { border-color: var(--color-primary); background: var(--el-color-primary-light-9); }
.instance-choice:focus-visible, .role-choice:focus-visible { outline: 3px solid var(--el-color-primary-light-5); outline-offset: 3px; }
.is-selected { border: 2px solid var(--color-primary); background: var(--el-color-primary-light-9); }
.instance-choice.is-selected { padding: 17px 23px; }
.instance-choice:disabled, .role-choice:disabled { cursor: not-allowed; }
.instance-choice:disabled:not(.is-selected) { opacity: .65; }
.instance-content { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.instance-content strong { color: var(--color-primary); font-size: 22px; overflow-wrap: anywhere; }
.instance-meta { display: flex; flex-wrap: wrap; gap: 4px 18px; color: var(--text-secondary); font-size: 13px; line-height: 1.6; }
.instance-meta span { overflow-wrap: anywhere; }
.instance-states { display: flex; align-items: center; flex-wrap: wrap; justify-content: flex-end; gap: 12px 24px; flex-shrink: 0; }
.selection-state { color: var(--color-primary); font-weight: 600; }
.role-section { display: flex; flex-direction: column; gap: 14px; border-top: 1px solid var(--glass-border-dark); padding-top: 20px; }
.required-mark { color: var(--color-danger); }
.role-options { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.role-choice { display: flex; align-items: center; gap: 14px; padding: 18px; min-height: 86px; min-width: 0; }
.role-choice.is-selected { padding: 17px; }
.role-choice > span { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.role-choice strong { font-size: 16px; }
.role-choice small { color: var(--text-secondary); font-size: 13px; }
.role-check { margin-left: auto; color: var(--color-primary); }
.connect-footer { flex-direction: row; align-items: center; justify-content: space-between; gap: 16px; padding: 18px 24px; }
.connect-summary { display: flex; flex-direction: column; gap: 6px; min-width: 0; overflow-wrap: anywhere; }
.connect-summary strong { font-size: 17px; }.connect-summary span { color: var(--text-secondary); font-size: 13px; }
.connect-button { min-height: 42px; flex-shrink: 0; }
@media (max-width: 900px) {
  .instance-choice { align-items: flex-start; flex-direction: column; gap: 14px; }
  .instance-states { justify-content: flex-start; }
}
@media (max-width: 700px) {
  .glass-card { padding: 18px; }
  .connection-form :deep(.el-form-item) { flex-direction: column; align-items: stretch; }
  .connection-form :deep(.el-form-item__label) { width: auto !important; justify-content: flex-start; padding: 0 0 6px; height: auto; line-height: 1.5; }
  .role-options { grid-template-columns: minmax(0, 1fr); }
  .connect-footer { align-items: stretch; flex-direction: column; }
  .connect-button { align-self: flex-start; }
}
</style>
