<template>
  <div class="management-page">
    <h2>服务器接入与诊断</h2>
    <el-alert :type="runtime.ready ? 'success' : 'warning'" :closable="false"
      :title="runtime.ready ? `AI 运行时就绪：${runtime.version}（${runtime.mode === 'legacy' ? '兼容模式' : '标准模式'}）` : 'AI 暂不可用，核心管理与备份可以继续使用'"
      :description="runtime.reason" />
    <div class="glass-card">
      <el-radio-group v-model="form.mode" @change="instances = []; selected = ''">
        <el-radio-button value="local">本机 Gitea</el-radio-button>
        <el-radio-button value="ssh">跨主机 Gitea</el-radio-button>
      </el-radio-group>
      <el-form label-width="130px">
        <template v-if="form.mode === 'ssh'">
          <el-alert title="先在目标宿主机为管理账号授权下面的公钥；使用系统 SSH 端口，不是 Git SSH 端口。账号需要 Docker 和备份文件操作权限。" type="info" :closable="false" />
          <p>请目标主机管理员将此公钥追加到管理账号的 ~/.ssh/authorized_keys，目录权限设为 700，文件权限设为 600。该账号须能直接执行 docker 命令；授权完成后，其余检查在本页面完成。</p>
          <el-input :model-value="publicKey" type="textarea" readonly :rows="4" />
          <div class="actions"><el-button @click="copyPublicKey">复制公钥</el-button><el-button @click="downloadPublicKey">下载公钥</el-button></div>
          <el-form-item label="目标主机"><el-input v-model="form.host" placeholder="Gitea 宿主机 IP 或域名" /></el-form-item>
          <el-form-item label="系统 SSH 端口"><el-input-number v-model="form.ssh_port" :min="1" :max="65535" /></el-form-item>
          <el-form-item label="管理账号"><el-input v-model="form.ssh_user" /></el-form-item>
          <el-button @click="trustHost" :loading="busy">核对主机指纹</el-button>
        </template>
        <el-form-item label="服务器名称"><el-input v-model="form.name" placeholder="可选，默认使用实例名称" /></el-form-item>
        <el-form-item label="Gitea 访问地址"><el-input v-model="form.gitea_url" placeholder="可选，默认从实例配置读取" /></el-form-item>
      </el-form>
      <el-button @click="discover" :loading="busy">发现并检查实例</el-button>
      <el-radio-group v-model="selected" class="instance-list">
        <el-radio v-for="item in instances" :key="item.id" :value="item.id">{{ item.name }} · HTTP {{ item.port }} · {{ item.running ? '运行中' : '未运行' }}</el-radio>
      </el-radio-group>
      <el-button type="primary" :disabled="!selected" :loading="busy" @click="connect">连接并添加服务器</el-button>
      <el-alert v-if="message" :title="message" :type="failed ? 'error' : 'success'" :closable="false" />
      <a v-if="lastDiagnostic" :href="`/api/management/diagnostics/operation/${lastDiagnostic}`">下载本次操作诊断</a>
    </div>
    <div class="glass-card">
      <div class="actions"><h3>安装与运行诊断</h3><el-button @click="loadReports">刷新</el-button></div>
      <el-table :data="reports">
        <el-table-column prop="标题" label="操作" />
        <el-table-column prop="状态" label="结果" />
        <el-table-column prop="id" label="操作编号" />
        <el-table-column label="诊断"><template #default="{ row }"><a :href="`/api/management/diagnostics/${row.kind}/${row.id}`">下载诊断包</a> <el-button link @click="showSteps(row)">步骤详情</el-button></template></el-table-column>
      </el-table>
    </div>
    <el-dialog append-to-body v-model="stepsVisible" title="操作步骤" width="90%"><el-table :data="steps"><el-table-column prop="时间" label="时间" /><el-table-column prop="步骤" label="步骤" /><el-table-column prop="状态" label="状态" /><el-table-column prop="说明" label="详情" /></el-table></el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { ElMessageBox, ElMessage } from 'element-plus'
import { api } from '../api'
const stepsVisible = ref(false), steps = ref([])
async function showSteps(row) { steps.value = (await api.get(`/management/diagnostics/${row.kind}/${row.id}/steps`)).data.events; stepsVisible.value = true }
const form = ref({ mode: 'local', host: '', ssh_port: 22, ssh_user: 'root', name: '', gitea_url: '' })
const runtime = ref({ ready: false }), publicKey = ref(''), csrf = ref(''), instances = ref([]), selected = ref('')
let runtimeTimer
onUnmounted(() => clearInterval(runtimeTimer))
const busy = ref(false), message = ref(''), failed = ref(false), lastDiagnostic = ref(''), reports = ref([])
const post = (url, body) => api.post('/management/' + url, body, { headers: { 'X-Management-CSRF': csrf.value } })
async function run(action) {
  busy.value = true; message.value = ''; lastDiagnostic.value = ''; failed.value = false
  try { await action() } catch (e) { failed.value = true; message.value = e.response?.data?.error || e.message; lastDiagnostic.value = e.response?.data?.diagnostic_id || '' }
  finally { busy.value = false; await loadReports() }
}
async function loadReports() { reports.value = (await api.get('/management/diagnostics')).data.reports }
function discover() { return run(async () => { const { data } = await post('discover', form.value); instances.value = data.instances; lastDiagnostic.value = data.diagnostic_id; message.value = `发现 ${instances.value.length} 个实例` }) }
function connect() { return run(async () => { const { data } = await post('connect', { ...form.value, instance_id: selected.value }); lastDiagnostic.value = data.diagnostic_id; message.value = '服务器已连接，可以在服务器管理和备份页面使用' }) }
function trustHost() { return run(async () => { const { data } = await post('ssh-fingerprint', form.value); await ElMessageBox.confirm(`请与目标管理员核对主机指纹：${data.fingerprint}`, '确认主机身份'); await post('ssh-trust', data); message.value = '主机指纹已固定，请继续发现实例' }) }
async function copyPublicKey() { try { await navigator.clipboard.writeText(publicKey.value); ElMessage.success('公钥已复制') } catch { ElMessage.info('当前浏览器不支持自动复制，请选择公钥文本复制') } }
function downloadPublicKey() { const url = URL.createObjectURL(new Blob([publicKey.value + '\n'], { type: 'text/plain' })); const link = document.createElement('a'); link.href = url; link.download = 'gitea-manager.pub'; link.click(); URL.revokeObjectURL(url) }
onMounted(async () => { runtimeTimer = setInterval(async () => { try { runtime.value = (await api.get('/management/status', { notifyError: false })).data.runtime } catch { runtime.value = { ready: false, reason: '暂时无法获取执行器状态，请检查服务连接' } } }, 10000); const { data } = await api.get('/management/status'); runtime.value = data.runtime; csrf.value = data.csrf; try { publicKey.value = (await api.get('/management/ssh-public-key')).data.public_key } catch (e) { message.value = e.response?.data?.error || e.message; failed.value = true } await loadReports(); if (form.value.mode === 'local') await discover() })
</script>

<style scoped>
.management-page { min-width: 0; display: flex; flex-direction: column; gap: 18px; }
.glass-card { padding: 20px; display: flex; flex-direction: column; gap: 16px; }
.actions { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
.instance-list :deep(.el-radio) { max-width: 100%; height: auto; white-space: normal; align-items: flex-start; }
.instance-list :deep(.el-radio__label) { white-space: normal; overflow-wrap: anywhere; }
.instance-list { display: flex; flex-direction: column; align-items: flex-start; gap: 8px; }
</style>
