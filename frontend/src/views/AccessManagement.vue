<template>
  <div class="access-page" v-loading="loading">
    <div class="access-heading"><div><h2>账号与权限</h2><p>公司目录验证身份，平台管理员分配管理组织。</p></div><el-button :icon="Refresh" @click="load">刷新</el-button></div>
    <el-alert v-if="error" :title="error" type="error" show-icon closable @close="error = ''" />
    <el-tabs v-model="tab">
      <el-tab-pane label="账号与组织授权" name="accounts">
        <div class="glass-card access-card">
          <div class="access-actions"><el-input v-model="query" placeholder="按用户名查询目录" aria-label="目录用户名" clearable @keyup.enter="searchDirectory" /><el-button :loading="searching" @click="searchDirectory">查询目录</el-button></div>
          <p class="muted">仅按需添加管理员；没有查询凭据的 Simple auth 目录用户可先登录，再在下表授权。</p>
          <el-table v-if="candidates.length" :data="candidates" empty-text="没有匹配账号"><el-table-column prop="username" label="目录账号" /><el-table-column prop="display_name" label="姓名" /><el-table-column label="操作" width="140"><template #default="{ row }"><el-button link type="primary" :loading="busy" @click="addAccount(row)">添加待授权账号</el-button></template></el-table-column></el-table>
          <el-table :data="accounts" stripe empty-text="尚无个人账号">
            <el-table-column label="账号" min-width="160"><template #default="{ row }"><strong>{{ row.display_name || row.username }}</strong><div class="muted">{{ row.username }} · {{ row.source === 'local' ? '本地' : '目录' }}</div></template></el-table-column>
            <el-table-column label="角色" width="140"><template #default="{ row }"><el-tag :type="row.is_platform_admin ? 'warning' : 'info'">{{ row.is_platform_admin ? '平台管理员' : row.grants?.length ? '组织管理员' : '待授权' }}</el-tag></template></el-table-column>
            <el-table-column label="组织授权" min-width="250"><template #default="{ row }"><span v-if="row.is_platform_admin">全部实例与组织</span><div v-else class="access-tags"><el-tag v-for="id in row.grants || []" :key="id" closable @close="removeGrant(row, id)">{{ organizationLabel(id) }}</el-tag><span v-if="!row.grants?.length" class="muted">未分配</span></div></template></el-table-column>
            <el-table-column label="状态" width="90"><template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'danger'">{{ row.enabled ? '启用' : '停用' }}</el-tag></template></el-table-column>
            <el-table-column label="操作" min-width="330"><template #default="{ row }"><div class="access-actions">
              <el-button v-if="!row.is_platform_admin" link type="primary" @click="openGrant(row)">授权组织</el-button>
              <el-button v-if="row.source !== 'local'" link @click="changeRole(row)">{{ row.is_platform_admin ? '撤销平台管理员' : '设为平台管理员' }}</el-button>
              <el-button v-if="row.source !== 'local'" link :type="row.enabled ? 'danger' : 'primary'" @click="updateAccount(row, { enabled: !row.enabled })">{{ row.enabled ? '停用账号' : '启用账号' }}</el-button>
              <el-button link @click="revokeSessions(row)">撤销登录会话</el-button>
            </div></template></el-table-column>
          </el-table>
        </div>
      </el-tab-pane>
      <el-tab-pane label="组织同步" name="organizations">
        <div class="glass-card access-card"><div class="access-actions"><el-select v-model="syncServerId" placeholder="选择 Gitea 实例" aria-label="同步组织的实例"><el-option v-for="server in servers" :key="server.id" :value="server.id" :label="server.name" /></el-select><el-button type="primary" :disabled="!syncServerId" :loading="busy" @click="syncOrganizations">同步已有组织</el-button></div><p class="muted">读取已有组织的稳定 ID；组织创建仍在 Gitea 中进行。实例恢复后需要重新核验授权。</p><el-table :data="organizations" stripe><el-table-column prop="server_name" label="实例" min-width="150" /><el-table-column prop="organization_name" label="组织" min-width="180" /><el-table-column prop="organization_id" label="组织 ID" width="120" /><el-table-column label="状态" min-width="160"><template #default="{ row }"><el-tag :type="row.active ? 'success' : 'warning'">{{ row.active ? '已核验' : '待核验 / 已失效' }}</el-tag></template></el-table-column></el-table></div>
      </el-tab-pane>
      <el-tab-pane label="LDAP / AD 设置" name="directory">
        <div class="glass-card access-card">
          <el-alert title="字段与 Gitea LDAP 认证设置对应，可逐项复用已验证的公司配置。LDAP 群组只限制登录；组织管理权限仍在此平台手动分配。" type="info" :closable="false" />
          <el-form label-position="top" @submit.prevent="saveDirectory">
            <div class="access-grid">
              <el-form-item label="认证名称 (Authentication Name)" required><el-input v-model="directory.name" /></el-form-item>
              <el-form-item label="认证类型 (Authentication Type)"><el-select v-model="directory.type"><el-option label="LDAP（通过 BindDN）" value="binddn" /><el-option label="LDAP（简单认证 / Simple auth）" value="simple" /></el-select></el-form-item>
              <el-form-item label="启用认证源"><el-switch v-model="directory.enabled" /></el-form-item>
              <el-form-item label="安全协议 (Security Protocol)"><el-select v-model="directory.security_protocol"><el-option label="LDAPS" value="ldaps" /><el-option label="StartTLS" value="starttls" /><el-option label="未加密 (Unencrypted)" value="unencrypted" /></el-select></el-form-item>
              <el-form-item label="主机 (Host)" required><el-input v-model="directory.host" placeholder="ldap.example.com（不含协议和路径）" /></el-form-item>
              <el-form-item label="端口 (Port)" required><el-input-number v-model="directory.port" :min="1" :max="65535" /></el-form-item>
              <el-form-item label="跳过 TLS 证书验证 (Skip TLS Verify)"><el-switch v-model="directory.skip_verify" /><span class="muted">默认关闭；按保存值执行，不自动降级。</span></el-form-item>
              <el-form-item label="企业 CA 证书 (PEM)"><el-input v-model="directory.ca_cert" type="textarea" :rows="3" placeholder="留空使用系统信任链" /></el-form-item>
              <template v-if="directory.type === 'binddn'">
                <el-form-item label="绑定 DN (Bind DN)"><el-input v-model="directory.bind_dn" autocomplete="off" placeholder="留空使用匿名查询" /></el-form-item>
                <el-form-item :label="directory.bind_password_set ? '绑定密码 (Bind Password) · 已设置，留空保留' : '绑定密码 (Bind Password)'"><el-input v-model="bindPassword" type="password" autocomplete="new-password" show-password /><el-checkbox v-if="directory.bind_password_set" v-model="clearBindPassword">清除已保存的绑定密码</el-checkbox></el-form-item>
              </template>
              <el-form-item v-else label="用户 DN 模板 (User DN)" required><el-input v-model="directory.user_dn" placeholder="uid=%s,ou=People,dc=example,dc=com" /></el-form-item>
              <el-form-item :label="directory.type === 'simple' ? '用户搜索基准 (User Search Base) · 可选' : '用户搜索基准 (User Search Base)'" :required="directory.type === 'binddn'"><el-input v-model="directory.user_base" /></el-form-item>
              <el-form-item label="用户过滤器 (User Filter)" required><el-input v-model="directory.filter" placeholder="(uid=%s)" /><span class="muted">支持 %s、%[1]s，查询值由服务端转义。</span></el-form-item>
              <el-form-item label="用户名属性 (Username Attribute)"><el-input v-model="directory.attribute_username" placeholder="留空采用登录名" /></el-form-item>
              <el-form-item label="名字属性 (First Name Attribute)"><el-input v-model="directory.attribute_name" /></el-form-item>
              <el-form-item label="姓氏属性 (Surname Attribute)"><el-input v-model="directory.attribute_surname" /></el-form-item>
              <el-form-item label="邮箱属性 (Email Attribute)"><el-input v-model="directory.attribute_mail" /></el-form-item>
              <el-form-item v-if="directory.type === 'binddn'" label="在 BindDN 上下文读取属性 (Fetch Attributes in BindDN Context)"><el-switch v-model="directory.attributes_in_bind" /></el-form-item>
              <el-form-item label="启用群组校验 (Verify Group Membership)"><el-switch v-model="directory.groups_enabled" /></el-form-item>
              <template v-if="directory.groups_enabled">
                <el-form-item label="群组搜索基准 (Group Search Base)" required><el-input v-model="directory.group_dn" /></el-form-item>
                <el-form-item label="群组过滤器 (Group Filter)"><el-input v-model="directory.group_filter" placeholder="cn=manager-users（不含外层括号）" /><span class="muted">填写格式与 Gitea 一致；留空不执行群组限制。</span></el-form-item>
                <el-form-item label="群组成员属性 (Group Member UID)" required><el-input v-model="directory.group_member_uid" placeholder="member / memberUid" /></el-form-item>
                <el-form-item label="用户对应属性 (User UID)" required><el-input v-model="directory.user_uid" placeholder="uid；填写 dn 使用用户完整 DN" /></el-form-item>
              </template>
              <el-form-item label="启用分页 (Enable LDAP Page Size)"><el-switch v-model="directory.paged_search_enabled" /></el-form-item>
              <el-form-item v-if="directory.paged_search_enabled" label="每页大小 (LDAP Page Size)"><el-input-number v-model="directory.paged_search_size" :min="1" :max="10000" /></el-form-item>
            </div>
            <div class="access-actions"><el-button type="primary" native-type="submit" :loading="busy">保存目录设置</el-button><el-button :loading="testing" @click="testConnection">测试当前连接配置</el-button><el-button @click="testVisible = true">测试用户登录</el-button></div>
          </el-form>
          <div v-if="testResult" class="test-result" role="status"><el-alert :title="testResult.message || (testResult.ok ? '测试通过' : '测试未通过')" :type="testResult.ok ? 'success' : 'error'" :closable="false" /><el-table v-if="testResult.steps?.length" :data="testResult.steps"><el-table-column prop="stage" label="阶段" width="180" /><el-table-column label="结果" width="90"><template #default="{ row }">{{ row.ok ? '通过' : '未通过' }}</template></el-table-column><el-table-column prop="message" label="说明" /></el-table></div>
        </div>
      </el-tab-pane>
    </el-tabs>
    <el-dialog v-model="grantVisible" append-to-body title="分配管理组织" width="min(600px, 94vw)"><p>账号：{{ grantAccount?.display_name || grantAccount?.username }}</p><el-select v-model="grantBindingId" filterable placeholder="选择已有组织" aria-label="授权组织" class="full"><el-option v-for="org in availableOrganizations" :key="org.binding_id" :value="org.binding_id" :label="organizationLabel(org.binding_id)" /></el-select><template #footer><el-button @click="grantVisible = false">取消</el-button><el-button type="primary" :disabled="!grantBindingId" :loading="busy" @click="saveGrant">添加授权</el-button></template></el-dialog>
    <el-dialog v-model="testVisible" append-to-body title="测试用户登录" width="min(480px, 94vw)" @closed="testPassword = ''"><el-form label-position="top" @submit.prevent="testLogin"><el-form-item label="公司目录用户名"><el-input v-model="testUsername" autocomplete="off" /></el-form-item><el-form-item label="密码（只用于本次测试）"><el-input v-model="testPassword" type="password" autocomplete="new-password" /></el-form-item><el-button type="primary" native-type="submit" :loading="testing">执行登录测试</el-button></el-form></el-dialog>
  </div>
</template>
<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { api } from '../api'
const tab = ref('accounts'), loading = ref(false), busy = ref(false), searching = ref(false), testing = ref(false), error = ref('')
const accounts = ref([]), organizations = ref([]), servers = ref([]), candidates = ref([]), query = ref(''), syncServerId = ref(null)
const grantVisible = ref(false), grantAccount = ref(null), grantBindingId = ref(null)
const bindPassword = ref(''), clearBindPassword = ref(false), testVisible = ref(false), testUsername = ref(''), testPassword = ref(''), testResult = ref(null)
const directory = reactive({ name: '公司 LDAP / AD', type: 'binddn', enabled: false, host: '', port: 636, security_protocol: 'ldaps', skip_verify: false, bind_dn: '', bind_password_set: false, user_base: '', user_dn: '', filter: '', attribute_username: '', attribute_name: '', attribute_surname: '', attribute_mail: '', attributes_in_bind: false, groups_enabled: false, group_dn: '', group_filter: '', group_member_uid: '', user_uid: '', paged_search_enabled: false, paged_search_size: 500, ca_cert: '' })
const items = data => Array.isArray(data) ? data : data?.items || []
const fail = e => { if (e.code !== 'ERR_CANCELED') error.value = e.response?.data?.error || e.message || '操作失败' }
const organizationLabel = id => { const org = organizations.value.find(o => Number(o.binding_id) === Number(id)); return org ? `${org.server_name} / ${org.organization_name}` : `失效组织 #${id}` }
const availableOrganizations = computed(() => organizations.value.filter(o => o.active && !(grantAccount.value?.grants || []).includes(o.binding_id)))
async function loadAccounts() { accounts.value = items((await api.get('/access/accounts')).data) }
async function loadOrganizations() { organizations.value = items((await api.get('/access/organizations')).data) }
async function load() { loading.value = true; error.value = ''; try { const [d, s] = await Promise.all([api.get('/access/directory'), api.get('/servers'), loadAccounts(), loadOrganizations()]); Object.assign(directory, d.data); bindPassword.value = ''; clearBindPassword.value = false; servers.value = items(s.data); syncServerId.value ||= servers.value[0]?.id } catch (e) { fail(e) } finally { loading.value = false } }
async function mutate(fn) { if (busy.value) return; busy.value = true; error.value = ''; try { await fn() } catch (e) { if (e !== 'cancel' && e !== 'close') fail(e) } finally { busy.value = false } }
function configuration() { const { id, bind_password_set, ...data } = directory; if (bindPassword.value) data.bind_password = bindPassword.value; else if (clearBindPassword.value) data.bind_password = ''; return data }
async function saveDirectory() { await mutate(async () => { Object.assign(directory, (await api.put('/access/directory', configuration())).data); bindPassword.value = ''; clearBindPassword.value = false; ElMessage.success('目录设置已保存') }) }
async function runTest(path, extra = {}) { if (testing.value) return; testing.value = true; testResult.value = null; error.value = ''; try { testResult.value = (await api.post('/access/directory/' + path, { configuration: configuration(), ...extra })).data } catch (e) { testResult.value = { ...e.response?.data, ok: false, message: e.response?.data?.error || '测试失败，请检查服务连接' } } finally { testing.value = false; testPassword.value = '' } }
const testConnection = () => runTest('test-connection')
async function testLogin() { await runTest('test-login', { username: testUsername.value, password: testPassword.value }); testVisible.value = false }
async function searchDirectory() { if (!query.value.trim()) return; searching.value = true; error.value = ''; candidates.value = []; try { candidates.value = items((await api.get('/access/directory/users', { params: { q: query.value.trim() } })).data) } catch (e) { fail(e) } finally { searching.value = false } }
async function addAccount(row) { await mutate(async () => { await api.post('/access/directory/users', { username: row.username }); await loadAccounts(); ElMessage.success('账号已加入，可分配组织') }) }
async function syncOrganizations() { await mutate(async () => { await api.post('/access/organizations', { server_id: syncServerId.value }); await loadOrganizations(); ElMessage.success('组织同步完成') }) }
function openGrant(row) { grantAccount.value = row; grantBindingId.value = null; grantVisible.value = true }
async function saveGrant() { await mutate(async () => { await api.post('/access/grants', { account_id: grantAccount.value.id, binding_id: grantBindingId.value }); grantVisible.value = false; await loadAccounts(); ElMessage.success('组织授权已生效') }) }
async function removeGrant(row, id) { await mutate(async () => { await ElMessageBox.confirm(`撤销 ${row.display_name || row.username} 对 ${organizationLabel(id)} 的管理权限？后续请求和未执行操作将重新校验。`, '撤销组织授权', { type: 'warning', confirmButtonText: '撤销授权', cancelButtonText: '取消' }); await api.delete(`/access/grants/${row.id}/${id}`); await loadAccounts() }) }
async function updateAccount(row, data) { await mutate(async () => { await api.patch(`/access/accounts/${row.id}`, data); await loadAccounts() }) }
async function changeRole(row) { try { await ElMessageBox.confirm(row.is_platform_admin ? '撤销平台角色后仅保留已分配的组织权限。' : '平台管理员可以访问全部实例、凭据和管理功能。', '变更平台管理员角色', { type: 'warning', confirmButtonText: '确认变更', cancelButtonText: '取消' }); await updateAccount(row, { is_platform_admin: !row.is_platform_admin }) } catch {} }
async function revokeSessions(row) { await mutate(async () => { await api.post('/access/sessions/revoke', { account_id: row.id }); ElMessage.success('该账号现有登录会话已撤销') }) }
onMounted(load)
onUnmounted(() => { bindPassword.value = ''; testPassword.value = '' })
</script>
<style scoped>
.access-page { display: grid; gap: 18px; min-width: 0; }
.access-heading, .access-actions, .access-tags { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; min-width: 0; }
.access-heading { justify-content: space-between; }
.access-heading h2 { margin: 0 0 8px; }
.access-heading p, .muted { color: var(--text-secondary); font-size: 13px; line-height: 1.6; }
.access-card { padding: 22px; min-width: 0; }
.access-card > .el-alert { margin-bottom: 22px; }
.access-actions > .el-input, .access-actions > .el-select { width: 300px; max-width: 100%; }
.access-actions :deep(.el-button + .el-button) { margin-left: 0; }
.access-tags :deep(.el-tag) { max-width: 100%; height: auto; white-space: normal; overflow-wrap: anywhere; }
.access-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 24px; }
.access-grid :deep(.el-select), .full { width: 100%; }
.access-grid .muted { display: block; width: 100%; margin-top: 6px; }
.test-result { margin-top: 20px; }
@media (max-width: 760px) { .access-grid { grid-template-columns: minmax(0, 1fr); } .access-card { padding: 14px; } }
</style>
