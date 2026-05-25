<template>
  <div v-loading="loading">
    <el-page-header @back="$router.push('/servers')" :title="server.name" style="margin-bottom:16px">
      <template #content>
        <span style="font-size:16px;font-weight:600">{{ server.name }}</span>
        <el-tag :type="server.role === 'primary' ? '' : 'info'" size="small" style="margin-left:8px">{{ server.role }}</el-tag>
        <el-tag :type="server.status === 'online' ? 'success' : 'danger'" size="small" effect="dark" style="margin-left:4px">{{ server.status }}</el-tag>
        <el-tag v-if="server.is_local" type="success" size="small" style="margin-left:4px">本地</el-tag>
        <el-tag v-else size="small" style="margin-left:4px">远程</el-tag>
      </template>
    </el-page-header>

    <el-row :gutter="14" style="margin-bottom:16px">
      <el-col :span="6"><div class="info-card"><div class="info-val">{{ server.version || '-' }}</div><div class="info-label">版本</div></div></el-col>
      <el-col :span="6"><div class="info-card"><div class="info-val">{{ server.repo_count }}</div><div class="info-label">仓库</div></div></el-col>
      <el-col :span="6"><div class="info-card"><div class="info-val">{{ server.user_count }}</div><div class="info-label">用户</div></div></el-col>
      <el-col :span="6"><div class="info-card"><div class="info-val">{{ server.gitea_port || '-' }}</div><div class="info-label">端口</div></div></el-col>
    </el-row>

    <el-row :gutter="14" style="margin-bottom:16px">
      <el-col :span="9"><div class="info-card"><div class="info-val font-mono">{{ detail.backup_count }}</div><div class="info-label">备份数</div></div></el-col>
      <el-col :span="9"><div class="info-card"><div class="info-val font-mono">{{ detail.restore_count }}</div><div class="info-label">恢复次数</div></div></el-col>
      <el-col :span="6"><div class="info-card"><div class="info-val">{{ detail.container ? detail.container.image : '-' }}</div><div class="info-label">镜像</div></div></el-col>
    </el-row>

    <div v-if="detail.resources" style="margin-bottom:16px">
      <el-tag size="small">CPU: {{ detail.resources.cpu_percent }}%</el-tag>
      <el-tag size="small" style="margin-left:8px">内存: {{ fmtMem(detail.resources.memory_used) }} / {{ fmtMem(detail.resources.memory_limit) }}</el-tag>
    </div>
    <div v-if="detail.disk" style="margin-bottom:16px">
      <el-tag size="small" type="warning">磁盘: {{ detail.disk }}</el-tag>
    </div>
    <div v-if="detail.container" style="margin-bottom:16px;color:#999;font-size:12px">
      容器: {{ detail.container.name }} | 状态: {{ detail.container.status }}
    </div>

    <el-card shadow="hover" style="margin-bottom:16px">
      <template #header>
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span style="font-weight:600">最近日志</span>
          <el-button size="small" @click="loadData">刷新</el-button>
        </div>
      </template>
      <pre v-if="detail.logs" class="log-block">{{ detail.logs }}</pre>
      <el-empty v-else description="暂无日志" :image-size="60" />
    </el-card>

    <el-collapse v-model="activeSections" style="margin-bottom:16px">
      <el-collapse-item name="backups" :title="`备份记录 (共 ${detail.backup_count} 条)`">
        <el-table :data="detail.backups || []" size="small" border v-if="(detail.backups || []).length">
          <el-table-column prop="id" label="ID" width="60" />
          <el-table-column prop="filename" label="文件名" min-width="220" show-overflow-tooltip />
          <el-table-column label="大小" width="90">
            <template #default="{ row }">{{ fmtSize(row.file_size) }}</template>
          </el-table-column>
          <el-table-column prop="status" label="状态" width="80">
            <template #default="{ row }">
              <el-tag :type="row.status === 'success' ? 'success' : 'danger'" size="small">{{ row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="时间" width="150">
            <template #default="{ row }">{{ fmt(row.started_at) }}</template>
          </el-table-column>
        </el-table>
        <el-empty v-else description="暂无备份" :image-size="60" />
      </el-collapse-item>

      <el-collapse-item name="restores" :title="`恢复记录 (共 ${detail.restore_count} 条)`">
        <el-table :data="detail.restores || []" size="small" border v-if="(detail.restores || []).length">
          <el-table-column prop="id" label="ID" width="60" />
          <el-table-column prop="backup_filename" label="备份文件" min-width="220" show-overflow-tooltip />
          <el-table-column prop="status" label="状态" width="80">
            <template #default="{ row }">
              <el-tag :type="row.status === 'success' ? 'success' : 'danger'" size="small">{{ row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="时间" width="150">
            <template #default="{ row }">{{ fmt(row.started_at) }}</template>
          </el-table-column>
        </el-table>
        <el-empty v-else description="暂无恢复记录" :image-size="60" />
      </el-collapse-item>
    </el-collapse>
  </div>
</template>

<script>
import { ref, reactive, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api'

export default {
  setup() {
    const route = useRoute()
    const loading = ref(false)
    const server = ref({})
    const detail = ref({})
    const activeSections = ref([])

    function loadData() {
      loading.value = true
      api.get(`/servers/${route.params.id}/detail`).then(res => {
        server.value = res.data
        detail.value = res.data.detail || {}
      }).finally(() => { loading.value = false })
    }

    function fmt(d) { return d ? new Date(d).toLocaleString() : '-' }
    function fmtSize(b) {
      if (!b) return '-'
      if (b < 1024) return b + 'B'
      if (b < 1048576) return (b / 1024).toFixed(1) + 'K'
      return (b / 1048576).toFixed(1) + 'M'
    }
    function fmtMem(b) {
      if (!b) return '-'
      if (b < 1048576) return (b / 1024).toFixed(0) + 'K'
      if (b < 1073741824) return (b / 1048576).toFixed(1) + 'M'
      return (b / 1073741824).toFixed(1) + 'G'
    }

    onMounted(loadData)
    return { loading, server, detail, activeSections, loadData, fmt, fmtSize, fmtMem }
  },
}
</script>

<style scoped>
.info-card { background: #fff; border-radius: 8px; padding: 14px 16px; text-align: center; border: 1px solid #ebeef5; }
.info-val { font-size: 22px; font-weight: 700; color: #303133; }
.info-val.font-mono { font-family: monospace; }
.info-label { font-size: 12px; color: #909399; margin-top: 4px; }
.log-block { background: #1a1d2e; color: #e0e0e0; padding: 14px; border-radius: 6px; font-size: 12px; line-height: 1.6; max-height: 280px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; margin: 0; }
</style>
