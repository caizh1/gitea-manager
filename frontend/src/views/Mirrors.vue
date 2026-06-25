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
          <span class="mirror-info-item">同步模式: {{ m.deprecated ? '旧 Pull Mirror（已弃用）' : 'Push Mirror' }}</span>
          <span class="mirror-info-item">Push 后立即同步: {{ m.sync_on_commit !== false ? '开启' : '关闭' }}</span>
          <span class="mirror-info-item">兜底间隔: {{ m.sync_interval }}分钟</span>
          <span class="mirror-info-item">仓库: {{ m.synced_repos }}/{{ m.total_repos }} 同步</span>
          <span v-if="m.failed_repos > 0" class="mirror-info-item mirror-fail">失败: {{ m.failed_repos }}</span>
          <span v-if="m.last_sync_at" class="mirror-info-item">最后同步: {{ fmt(m.last_sync_at) }}</span>
        </div>
        <el-alert
          v-if="m.deprecated"
          type="warning"
          :closable="false"
          show-icon
          title="旧 Pull Mirror / Git Clone 配置已弃用，请删除后重新创建 Push Mirror。"
          style="margin-bottom:14px"
        />
        <pre v-if="mirrorFailureLog(m)" class="mirror-log">{{ mirrorFailureLog(m) }}</pre>
        <div class="mirror-card-actions">
          <el-button size="small" type="primary" @click="doSync(m.id)" :disabled="m.status === 'syncing' || m.deprecated">
            {{ m.status === 'syncing' ? '同步中...' : '同步' }}
          </el-button>
          <el-button size="small" @click="showDetail(m)">查看详情</el-button>
          <el-button size="small" @click="editMirror(m)" :disabled="m.deprecated">编辑</el-button>
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
        <el-form-item label="镜像模式">
          <el-tag type="success" effect="dark">Push Mirror</el-tag>
        </el-form-item>
        <el-form-item label="Push 后同步">
          <el-switch v-model="form.sync_on_commit" active-text="开启" inactive-text="关闭" />
        </el-form-item>
        <el-form-item label="兜底间隔(分钟)">
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
        <button class="icon-btn-sm" @click="loadDetailRepo" title="刷新" aria-label="刷新">
          <el-icon><Refresh /></el-icon>
        </button>
      </div>
      <pre v-if="detailSyncLog" class="mirror-log detail-log">{{ detailSyncLog }}</pre>
      <el-table :data="detailRepos" stripe style="width:100%" max-height="400">
        <el-table-column prop="repo_name" label="仓库" min-width="250" />
        <el-table-column prop="status" label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'success' ? 'success' : row.status === 'failed' ? 'danger' : 'info'" size="small">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="sync_mode" label="模式" width="110">
          <template #default="{ row }">
            {{ row.sync_mode === 'push_mirror' ? 'Push Mirror' : row.sync_mode || '旧配置' }}
          </template>
        </el-table-column>
        <el-table-column prop="last_sync_at" label="最后同步" width="160">
          <template #default="{ row }">{{ fmt(row.last_sync_at) }}</template>
        </el-table-column>
        <el-table-column prop="error_msg" label="错误" min-width="150" />
        <el-table-column label="操作" width="80">
          <template #default="{ row }">
            <el-button v-if="row.status === 'failed' && !detailDeprecated" size="small" text type="primary" @click="syncOneRepo(row)">重试</el-button>
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
    const detailDeprecated = ref(false)
    const detailSyncLog = ref('')
    const detailRepos = ref([])

    const form = ref({
      source_server_id: null,
      target_server_id: null,
      sync_interval: 30,
      sync_on_commit: true,
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
        sync_interval: form.value.sync_interval,
        sync_on_commit: form.value.sync_on_commit,
      }).then(() => {
        ElMessage.success('已更新')
        showCreateDialog.value = false
        editingMirror.value = null
        load()
      })
    }

    function editMirror(m) {
      if (m.deprecated) {
        ElMessage.warning('旧镜像配置已弃用，请删除后重新创建 Push Mirror')
        return
      }
      editingMirror.value = m
      form.value = {
        source_server_id: m.source_server_id,
        target_server_id: m.target_server_id,
        sync_interval: m.sync_interval,
        sync_on_commit: m.sync_on_commit,
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
      detailDeprecated.value = !!m.deprecated
      detailSyncLog.value = mirrorFailureLog(m)
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
      form.value = { source_server_id: null, target_server_id: null, sync_interval: 30, sync_on_commit: true }
    }

    function fmt(d) { return d ? new Date(d).toLocaleString() : '-' }

    function mirrorFailureLog(m) {
      if (!m || !m.last_sync_log) return ''
      if (!['failed', 'partial'].includes(m.status) && m.last_sync_status !== 'failed') return ''
      return m.last_sync_log
    }

    onMounted(load)

    return { mirrors, servers, showCreateDialog, editingMirror, form, detailVisible, detailRepos,
             detailSyncLog,
             primaryServers, nonPrimaryServers,
             load, createMirror, updateMirror, editMirror, deleteMirror, doSync,
             showDetail, loadDetailRepo, syncOneRepo, resetForm, fmt, detailDeprecated, mirrorFailureLog }
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
.mirror-log {
  margin: 0 0 14px; padding: 12px; border-radius: 8px; max-height: 180px; overflow: auto;
  background: rgba(239,68,68,0.06); border: 1px solid rgba(239,68,68,0.14);
  color: #7f1d1d; font-size: 12px; line-height: 1.5; white-space: pre-wrap; word-break: break-word;
}
.detail-log { max-height: 140px; }

.detail-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;
  font-weight: 600; font-size: 15px; color: #1a1a2e;
}
.icon-btn-sm {
  width: 30px; height: 30px; border-radius: 8px; border: 1px solid rgba(0,0,0,0.06);
  background: var(--glass-control); cursor: pointer; font-size: 14px; color: var(--text-secondary);
  box-shadow: inset 0 1px 0 var(--glass-highlight), var(--shadow-xs);
  transition: background 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease, color 0.18s ease, transform 0.18s ease;
  display: inline-flex; align-items: center; justify-content: center;
}
.icon-btn-sm:hover { background: var(--glass-surface-hover); border-color: rgba(0,122,255,0.18); color: var(--color-primary); transform: rotate(90deg); }
</style>
