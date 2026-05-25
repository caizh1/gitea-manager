<template>
  <div>
    <div class="section-header">
      <h3 class="section-title">镜像管理</h3>
      <el-button type="primary" size="small" @click="showCreateDialog = true">新建镜像配置</el-button>
    </div>

    <div v-if="mirrors.length === 0" class="glass-card" style="padding:40px;text-align:center;">
      <el-empty description="暂无镜像配置">
        <el-button type="primary" @click="showCreateDialog = true">创建镜像</el-button>
      </el-empty>
    </div>

    <div v-else class="mirror-list">
      <div v-for="m in mirrors" :key="m.id" class="glass-card mirror-card">
        <div class="mirror-card-header">
          <div class="mirror-path">
            <span class="mirror-server-name">{{ m.source_server_name }}</span>
            <span class="mirror-arrow">→</span>
            <span class="mirror-server-name">{{ m.target_server_name }}</span>
          </div>
          <div class="mirror-status-area">
            <el-tag v-if="m.status === 'success'" type="success" size="small" effect="dark">正常</el-tag>
            <el-tag v-else-if="m.status === 'partial'" type="warning" size="small" effect="dark">部分失败</el-tag>
            <el-tag v-else-if="m.status === 'syncing'" type="warning" size="small" effect="dark">同步中</el-tag>
            <el-tag v-else-if="m.status === 'failed'" type="danger" size="small" effect="dark">失败</el-tag>
            <el-tag v-else type="info" size="small" effect="dark">{{ m.status }}</el-tag>
          </div>
        </div>
        <div class="mirror-card-info">
          <span class="mirror-info-item">同步模式: {{ m.sync_mode === 'gitea_mirror' ? 'Gitea Mirror' : 'Git Clone' }}</span>
          <span v-if="m.sync_mode === 'gitea_mirror'" class="mirror-info-item">间隔: {{ m.sync_interval }}分钟</span>
          <span class="mirror-info-item">仓库: {{ m.synced_repos }}/{{ m.total_repos }} 同步</span>
          <span v-if="m.failed_repos > 0" class="mirror-info-item mirror-fail">失败: {{ m.failed_repos }}</span>
          <span v-if="m.last_sync_at" class="mirror-info-item">最后同步: {{ fmt(m.last_sync_at) }}</span>
        </div>
        <div class="mirror-card-actions">
          <el-button size="small" type="primary" @click="doSync(m.id)" :disabled="m.status === 'syncing'">
            {{ m.status === 'syncing' ? '同步中...' : '同步' }}
          </el-button>
          <el-button size="small" @click="showDetail(m)">查看详情</el-button>
          <el-button size="small" @click="editMirror(m)">编辑</el-button>
          <el-popconfirm title="确认删除此镜像配置?" @confirm="deleteMirror(m.id)">
            <template #reference>
              <el-button size="small" type="danger">删除</el-button>
            </template>
          </el-popconfirm>
        </div>
      </div>
    </div>

    <el-dialog v-model="showCreateDialog" :title="editingMirror ? '编辑镜像配置' : '新建镜像配置'" width="520px" @close="resetForm">
      <el-form label-width="110px">
        <el-form-item label="源服务器">
          <el-select v-model="form.source_server_id" placeholder="选择主服务器" style="width:100%" :disabled="!!editingMirror">
            <el-option v-for="s in primaryServers" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="目标服务器">
          <el-select v-model="form.target_server_id" placeholder="选择镜像服务器" style="width:100%" :disabled="!!editingMirror">
            <el-option v-for="s in nonPrimaryServers" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="同步模式">
          <el-radio-group v-model="form.sync_mode">
            <el-radio value="gitea_mirror">Gitea Mirror</el-radio>
            <el-radio value="git_clone">Git Clone</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="form.sync_mode === 'gitea_mirror'" label="同步间隔(分钟)">
          <el-input-number v-model="form.sync_interval" :min="5" :max="1440" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreateDialog = false">取消</el-button>
        <el-button v-if="!editingMirror" type="primary" @click="createMirror" :disabled="!form.source_server_id || !form.target_server_id">创建</el-button>
        <el-button v-else type="primary" @click="updateMirror">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="detailVisible" title="镜像仓库详情" width="700px">
      <div class="detail-header">
        <span>仓库状态列表</span>
        <button class="icon-btn-sm" @click="loadDetailRepo" title="刷新">↻</button>
      </div>
      <el-table :data="detailRepos" stripe style="width:100%" max-height="400">
        <el-table-column prop="repo_name" label="仓库" min-width="250" />
        <el-table-column prop="status" label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'success' ? 'success' : row.status === 'failed' ? 'danger' : 'info'" size="small">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="sync_mode" label="模式" width="110">
          <template #default="{ row }">
            {{ row.sync_mode === 'gitea_mirror' ? 'Gitea Mirror' : row.sync_mode === 'git_clone' ? 'Git Clone' : row.sync_mode || '-' }}
          </template>
        </el-table-column>
        <el-table-column prop="last_sync_at" label="最后同步" width="160">
          <template #default="{ row }">{{ fmt(row.last_sync_at) }}</template>
        </el-table-column>
        <el-table-column prop="error_msg" label="错误" min-width="150" />
        <el-table-column label="操作" width="80">
          <template #default="{ row }">
            <el-button v-if="row.status === 'failed'" size="small" text type="primary" @click="syncOneRepo(row)">重试</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>
  </div>
</template>

<script>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
import { ElMessage } from 'element-plus'

export default {
  setup() {
    const mirrors = ref([])
    const servers = ref([])
    const showCreateDialog = ref(false)
    const editingMirror = ref(null)
    const detailVisible = ref(false)
    const detailConfigId = ref(null)
    const detailRepos = ref([])

    const form = ref({
      source_server_id: null,
      target_server_id: null,
      sync_mode: 'gitea_mirror',
      sync_interval: 30,
    })

    const primaryServers = computed(() => servers.value.filter(s => s.role === 'primary'))
    const nonPrimaryServers = computed(() => servers.value.filter(s => s.role !== 'primary'))

    function load() {
      Promise.all([api.get('/mirrors'), api.get('/servers')])
        .then(([mRes, sRes]) => {
          mirrors.value = mRes.data
          servers.value = sRes.data
        })
    }

    function createMirror() {
      api.post('/mirrors', form.value).then(res => {
        ElMessage.success('镜像配置已创建')
        showCreateDialog.value = false
        resetForm()
        load()
        api.post(`/mirrors/${res.data.id}/setup`).then(() => {
          ElMessage.info('开始创建镜像仓库...')
          setTimeout(load, 5000)
        })
      })
    }

    function updateMirror() {
      if (!editingMirror.value) return
      api.put(`/mirrors/${editingMirror.value.id}`, {
        sync_mode: form.value.sync_mode,
        sync_interval: form.value.sync_interval,
      }).then(() => {
        ElMessage.success('已更新')
        showCreateDialog.value = false
        editingMirror.value = null
        load()
      })
    }

    function editMirror(m) {
      editingMirror.value = m
      form.value = {
        source_server_id: m.source_server_id,
        target_server_id: m.target_server_id,
        sync_mode: m.sync_mode,
        sync_interval: m.sync_interval,
      }
      showCreateDialog.value = true
    }

    function deleteMirror(id) {
      api.delete(`/mirrors/${id}`).then(() => {
        ElMessage.success('已删除')
        load()
      })
    }

    function doSync(id) {
      api.post(`/mirrors/${id}/sync`).then(() => {
        ElMessage.info('同步已触发')
        setTimeout(load, 5000)
      })
    }

    function showDetail(m) {
      detailConfigId.value = m.id
      detailVisible.value = true
      loadDetailRepo()
    }

    function loadDetailRepo() {
      if (!detailConfigId.value) return
      api.get(`/mirrors/${detailConfigId.value}/status`).then(res => {
        detailRepos.value = res.data
      })
    }

    function syncOneRepo(repo) {
      api.post(`/mirrors/${detailConfigId.value}/sync-repo/${repo.repo_name}`).then(() => {
        ElMessage.success('同步已触发')
        setTimeout(loadDetailRepo, 3000)
      })
    }

    function resetForm() {
      editingMirror.value = null
      form.value = { source_server_id: null, target_server_id: null, sync_mode: 'gitea_mirror', sync_interval: 30 }
    }

    function fmt(d) { return d ? new Date(d).toLocaleString() : '-' }

    onMounted(load)

    return { mirrors, servers, showCreateDialog, editingMirror, form, detailVisible, detailRepos,
             primaryServers, nonPrimaryServers,
             load, createMirror, updateMirror, editMirror, deleteMirror, doSync,
             showDetail, loadDetailRepo, syncOneRepo, resetForm, fmt }
  },
}
</script>

<style scoped>
.section-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px;
}
.section-title { font-size: 18px; font-weight: 700; color: #1a1a2e; margin: 0; }

.mirror-list { display: flex; flex-direction: column; gap: 16px; }
.mirror-card { padding: 22px; border-radius: 14px; }
.mirror-card-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;
}
.mirror-path { display: flex; align-items: center; gap: 10px; }
.mirror-server-name { font-size: 16px; font-weight: 700; color: #1a1a2e; }
.mirror-arrow { font-size: 18px; color: var(--color-primary); font-weight: 700; }
.mirror-card-info { display: flex; flex-wrap: wrap; gap: 16px; margin-bottom: 14px; }
.mirror-info-item { font-size: 13px; color: #6b7280; }
.mirror-fail { color: #ef4444 !important; font-weight: 600; }
.mirror-card-actions { display: flex; gap: 8px; }

.detail-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;
  font-weight: 600; font-size: 15px; color: #1a1a2e;
}
.icon-btn-sm {
  width: 30px; height: 30px; border-radius: 8px; border: 1px solid rgba(0,0,0,0.06);
  background: rgba(255,255,255,0.5); cursor: pointer; font-size: 14px;
  transition: all 0.25s; display: flex; align-items: center; justify-content: center;
}
.icon-btn-sm:hover { background: rgba(255,255,255,0.85); transform: rotate(90deg); }
</style>
