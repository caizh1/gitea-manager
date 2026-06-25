<template>
  <div v-loading="loading">
    <div class="detail-header">
      <button class="back-btn" @click="$router.push('/servers')">← 返回</button>
      <span class="detail-title">{{ server.name }}</span>
      <el-tag :type="server.role === 'primary' ? 'warning' : 'info'" size="small">{{ server.role }}</el-tag>
      <el-tag :type="server.status === 'online' ? 'success' : 'danger'" size="small" effect="dark">{{ server.status }}</el-tag>
      <el-tag v-if="server.is_local" type="success" size="small">本地</el-tag>
      <el-tag v-else size="small">远程</el-tag>
    </div>

    <div class="info-grid">
      <div class="glass-card info-card">
        <div class="info-val">{{ server.version || '-' }}</div>
        <div class="info-label">版本</div>
      </div>
      <div class="glass-card info-card">
        <div class="info-val">{{ server.repo_count }}</div>
        <div class="info-label">仓库</div>
      </div>
      <div class="glass-card info-card">
        <div class="info-val">{{ server.user_count }}</div>
        <div class="info-label">用户</div>
      </div>
      <div class="glass-card info-card">
        <div class="info-val">{{ server.gitea_port || '-' }}</div>
        <div class="info-label">端口</div>
      </div>
    </div>

    <div class="info-grid" style="grid-template-columns: 1fr 1fr 1fr;">
      <div class="glass-card info-card">
        <div class="info-val font-mono">{{ detail.backup_count }}</div>
        <div class="info-label">备份数</div>
      </div>
      <div class="glass-card info-card">
        <div class="info-val font-mono">{{ detail.restore_count }}</div>
        <div class="info-label">恢复次数</div>
      </div>
      <div class="glass-card info-card">
        <div class="info-val" style="font-size:16px;">{{ detail.container ? detail.container.image : '-' }}</div>
        <div class="info-label">镜像</div>
      </div>
    </div>

    <div v-if="detail.resources" class="resource-tags">
      <el-tag size="small" type="info" effect="plain">CPU: {{ detail.resources.cpu_percent }}%</el-tag>
      <el-tag size="small" type="warning" effect="plain">内存: {{ fmtMem(detail.resources.memory_used) }} / {{ fmtMem(detail.resources.memory_limit) }}</el-tag>
    </div>
    <div v-if="detail.disk" class="resource-tags">
      <el-tag size="small" type="danger" effect="plain">磁盘: {{ detail.disk }}</el-tag>
    </div>
    <div v-if="detail.container" class="container-info">
      容器: {{ detail.container.name }} | 状态: {{ detail.container.status }}
    </div>

    <div class="glass-card" style="padding:20px;margin-bottom:18px;">
      <div class="card-section-header">
        <span class="card-section-title">最近日志</span>
        <button class="icon-btn-sm" @click="loadData" title="刷新" aria-label="刷新">
          <el-icon><Refresh /></el-icon>
        </button>
      </div>
      <pre v-if="detail.logs" class="log-block">{{ detail.logs }}</pre>
      <el-empty v-else description="暂无日志" :image-size="60" />
    </div>

    <el-collapse v-model="activeSections" class="detail-collapse">
      <el-collapse-item name="backups" :title="`备份记录 (共 ${detail.backup_count} 条)`">
        <el-table :data="detail.backups || []" size="small" v-if="(detail.backups || []).length">
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
        <el-table :data="detail.restores || []" size="small" v-if="(detail.restores || []).length">
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
.detail-header {
  display: flex; align-items: center; gap: 12px; margin-bottom: 20px;
  animation: fadeInUp 0.22s ease both;
}
.back-btn {
  padding: 6px 14px; border-radius: 8px; border: 1px solid rgba(0,0,0,0.06);
  background: rgba(255,255,255,0.5); cursor: pointer; font-size: 13px; color: #6b7280;
  transition: background-color 0.18s ease, border-color 0.18s ease, color 0.18s ease; font-family: inherit;
  box-shadow: inset 0 1px 0 var(--glass-highlight), var(--shadow-xs);
}
.back-btn:hover { background: rgba(255,255,255,0.85); color: #1a1a2e; }
.detail-title { font-size: 18px; font-weight: 700; color: #1a1a2e; }

.info-grid {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 16px;
}
.info-card { padding: 18px; text-align: center; }
.info-val { font-size: 24px; font-weight: 700; color: #1a1a2e; letter-spacing: -0.5px; }
.info-val.font-mono { font-family: 'SF Mono', 'Fira Code', monospace; }
.info-label { font-size: 12px; color: #9ca3af; margin-top: 4px; font-weight: 500; }

.resource-tags { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.container-info { margin-bottom: 16px; color: #9ca3af; font-size: 12px; }

.card-section-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;
}
.card-section-title { font-weight: 600; font-size: 15px; color: #1a1a2e; }
.icon-btn-sm {
  width: 30px; height: 30px; border-radius: 8px; border: 1px solid rgba(0,0,0,0.06);
  background: var(--glass-control); cursor: pointer; font-size: 14px; color: var(--text-secondary);
  box-shadow: inset 0 1px 0 var(--glass-highlight), var(--shadow-xs);
  transition: background 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease, color 0.18s ease, transform 0.18s ease;
  display: inline-flex; align-items: center; justify-content: center;
}
.icon-btn-sm:hover { background: var(--glass-surface-hover); border-color: rgba(0,122,255,0.18); color: var(--color-primary); transform: rotate(90deg); }

.log-block {
  background: rgba(30,32,48,0.88);
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 10px; padding: 16px; font-family: 'SF Mono', 'Fira Code', monospace;
  font-size: 12px; line-height: 1.7; color: #c9d1d9;
  max-height: 280px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; margin: 0;
}

.detail-collapse {
  margin-bottom: 16px;
}
</style>
