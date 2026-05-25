<template>
  <div>
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <div style="display:flex;align-items:center;gap:8px">
        <h3 style="margin:0">备份管理</h3>
        <el-button circle size="small" @click="load" title="刷新">↻</el-button>
      </div>
      <el-button type="primary" @click="showCreateDialog = true" :disabled="primaryServers.length === 0">创建备份</el-button>
    </div>

    <div v-if="backups.length === 0">
      <el-empty description="暂无备份记录" />
    </div>

    <el-table v-else :data="backups" border stripe v-loading="loading">
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column prop="source_server_name" label="来源服务器" width="150" />
      <el-table-column prop="filename" label="文件名" min-width="280" />
      <el-table-column label="大小" width="100">
        <template #default="{ row }">{{ formatSize(row.file_size) }}</template>
      </el-table-column>
      <el-table-column prop="status" label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)" size="small">{{ row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="started_at" label="开始时间" width="170">
        <template #default="{ row }">{{ fmt(row.started_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-button size="small" type="primary" v-if="row.status === 'success'"
            @click="downloadBackup(row)">下载</el-button>
          <el-popconfirm title="确定删除此备份?" @confirm="deleteBackup(row)">
            <template #reference><el-button size="small" type="danger">删除</el-button></template>
          </el-popconfirm>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog title="创建备份" v-model="showCreateDialog" width="450px">
      <el-form label-width="100px">
        <el-form-item label="源服务器">
          <el-select v-model="selectedServerId" placeholder="选择主服务器" style="width:100%">
            <el-option v-for="s in primaryServers" :key="s.id" :label="`${s.name} (${s.host}:${s.gitea_port})`" :value="s.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreateDialog = false">取消</el-button>
        <el-button type="primary" @click="createBackup" :loading="creating">开始备份</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script>
import { ref, onMounted } from 'vue'
import { api } from '../api'

export default {
  setup() {
    const backups = ref([])
    const servers = ref([])
    const loading = ref(false)
    const creating = ref(false)
    const showCreateDialog = ref(false)
    const selectedServerId = ref(null)

    const primaryServers = ref([])

    function load() {
      loading.value = true
      Promise.all([api.get('/backups'), api.get('/servers')]).then(([bRes, sRes]) => {
        backups.value = bRes.data
        servers.value = sRes.data
        primaryServers.value = sRes.data.filter(s => s.role === 'primary')
      }).finally(() => { loading.value = false })
    }

    function createBackup() {
      if (!selectedServerId.value) return
      creating.value = true
      api.post('/backups', { source_server_id: selectedServerId.value }).then(() => {
        showCreateDialog.value = false
        selectedServerId.value = null
        load()
      }).finally(() => { creating.value = false })
    }

    function downloadBackup(row) {
      window.open(`/api/backups/${row.id}/download`, '_blank')
    }

    function deleteBackup(row) {
      api.delete(`/backups/${row.id}`).then(() => { load() })
    }

    function formatSize(bytes) {
      if (!bytes) return '-'
      if (bytes < 1024) return bytes + ' B'
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
      return (bytes / (1024 * 1024)).toFixed(1) + ' MB'
    }

    function statusType(s) {
      if (s === 'success') return 'success'
      if (s === 'running') return 'warning'
      return 'danger'
    }

    function fmt(d) { return d ? new Date(d).toLocaleString() : '-' }

    onMounted(load)

    return { backups, loading, creating, showCreateDialog, selectedServerId, primaryServers,
             load, createBackup, downloadBackup, deleteBackup, formatSize, statusType, fmt }
  },
}
</script>
