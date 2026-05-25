<template>
  <div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <div style="display:flex;align-items:center;gap:8px">
        <h3 style="margin:0">服务器管理</h3>
        <el-button circle size="small" @click="loadServers" title="刷新">↻</el-button>
      </div>
      <el-button type="primary" @click="openDialog()">添加服务器</el-button>
    </div>

    <el-alert
      v-if="checkMsg"
      :title="checkMsg.text"
      :type="checkMsg.ok ? 'success' : 'error'"
      closable
      @close="checkMsg = null"
      style="margin-bottom:12px"
    />

    <el-table :data="servers" border stripe v-loading="loading">
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column prop="name" label="名称" width="150">
        <template #default="{ row }">
          <span class="clickable-name" @click="$router.push('/servers/' + row.id)">{{ row.name }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="role" label="角色" width="90">
        <template #default="{ row }">
          <el-tag :type="row.role === 'primary' ? 'warning' : 'info'" size="small">{{ row.role }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="host" label="主机" width="150" />
      <el-table-column prop="gitea_port" label="端口" width="70" />
      <el-table-column prop="is_local" label="类型" width="70">
        <template #default="{ row }">
          <el-tag :type="row.is_local ? 'success' : ''" size="small">{{ row.is_local ? '本地' : '远程' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="status" label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="row.status === 'online' ? 'success' : 'danger'" size="small">{{ row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="version" label="版本" width="100" />
      <el-table-column prop="repo_count" label="仓库数" width="80" />
      <el-table-column prop="user_count" label="用户数" width="80" />
      <el-table-column label="操作" width="280" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click="checkServer(row)">测试连接</el-button>
          <el-button size="small" @click="openDialog(row)">编辑</el-button>
          <el-popconfirm title="确定删除?" @confirm="deleteServer(row)">
            <template #reference><el-button size="small" type="danger">删除</el-button></template>
          </el-popconfirm>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog :title="isEdit ? '编辑服务器' : '添加服务器'" v-model="dialogVisible" width="650px" destroy-on-close>
      <el-form :model="form" label-width="130px">
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="名称"><el-input v-model="form.name" /></el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="角色">
              <el-select v-model="form.role"><el-option label="primary" value="primary" /><el-option label="backup" value="backup" /></el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="16">
          <el-col :span="16">
            <el-form-item label="主机IP"><el-input v-model="form.host" placeholder="192.168.1.10" /></el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="SSH端口"><el-input v-model.number="form.ssh_port" type="number" min="1" max="65535" /></el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="SSH用户"><el-input v-model="form.ssh_user" placeholder="root" /></el-form-item>
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="Gitea端口"><el-input v-model.number="form.gitea_port" type="number" min="1" max="65535" /></el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="Gitea容器"><el-input v-model="form.gitea_container" placeholder="gitea" /></el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="PG容器"><el-input v-model="form.pg_container" placeholder="gitea-postgres" /></el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="PG数据库"><el-input v-model="form.pg_dbname" placeholder="gitea" /></el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="PG用户"><el-input v-model="form.pg_user" placeholder="gitea" /></el-form-item>
        <el-form-item label="Gitea URL"><el-input v-model="form.gitea_url" placeholder="http://192.168.1.10:3000" /></el-form-item>
        <el-form-item label="API Token"><el-input v-model="form.api_token" type="password" show-password placeholder="Gitea API Token" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveServer" :loading="saving">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script>
import { ref, onMounted } from 'vue'
import { api } from '../api'

export default {
  setup() {
    const servers = ref([])
    const loading = ref(false)
    const saving = ref(false)
    const dialogVisible = ref(false)
    const isEdit = ref(false)
    const editingId = ref(null)
    const checkMsg = ref(null)

    const defaultForm = {
      name: '', role: 'backup', host: '', ssh_port: 22, ssh_user: 'root',
      gitea_container: 'gitea', pg_container: 'gitea-postgres',
      pg_dbname: 'gitea', pg_user: 'gitea',
      gitea_port: 3000, gitea_url: '', api_token: '',
    }
    const form = ref({ ...defaultForm })

    function loadServers() {
      loading.value = true
      api.get('/servers').then(res => { servers.value = res.data }).finally(() => { loading.value = false })
    }

    function openDialog(row) {
      if (row) {
        isEdit.value = true
        editingId.value = row.id
        form.value = { ...row }
      } else {
        isEdit.value = false
        editingId.value = null
        form.value = { ...defaultForm }
      }
      dialogVisible.value = true
    }

    function saveServer() {
      saving.value = true
      const data = { ...form.value }
      const req = isEdit.value
        ? api.put(`/servers/${editingId.value}`, data)
        : api.post('/servers', data)
      req.then(() => {
        dialogVisible.value = false
        loadServers()
      }).finally(() => { saving.value = false })
    }

    function deleteServer(row) {
      api.delete(`/servers/${row.id}`).then(() => { loadServers() })
    }

    function checkServer(row) {
      api.post(`/servers/${row.id}/check`).then(res => {
        const s = servers.value.find(i => i.id === row.id)
        if (s) s.status = res.data.status
        checkMsg.value = {
          ok: res.data.ok,
          text: res.data.ok ? `${row.name} — 连接成功` : `${row.name} — 连接失败: ${res.data.message}`
        }
      }).catch(() => {
        checkMsg.value = { ok: false, text: '测试连接请求失败' }
      })
    }

    onMounted(loadServers)

    return { servers, loading, saving, dialogVisible, isEdit, form, checkMsg, loadServers, openDialog, saveServer, deleteServer, checkServer }
  },
}
</script>

<style>
.clickable-name { color: #409eff; cursor: pointer; }
.clickable-name:hover { text-decoration: underline; }
</style>
