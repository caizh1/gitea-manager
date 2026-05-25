<template>
  <div>
    <div class="section-header">
      <div class="header-left">
        <el-button size="small" @click="goBack" text>
          <el-icon><ArrowLeft /></el-icon> 返回统计
        </el-button>
        <h3 class="section-title">作者贡献排行</h3>
      </div>
      <div class="header-actions">
        <el-input v-model="searchText" placeholder="搜索作者" size="small" style="width:180px" clearable prefix-icon="Search" />
        <el-select v-model="sortBy" size="small" style="width:120px" @change="loadData">
          <el-option label="提交数" value="commits" />
          <el-option label="新增行" value="additions" />
          <el-option label="仓库数" value="repos" />
        </el-select>
      </div>
    </div>

    <div class="glass-card" style="padding:0;">
      <el-table :data="filteredAuthors" stripe style="width:100%" @row-click="goDetail" highlight-current-row size="small">
        <el-table-column label="#" width="50" align="center">
          <template #default="{ $index }">
            <span class="rank-badge" :class="{ 'rank-top': $index < 3 }">{{ $index + 1 }}</span>
          </template>
        </el-table-column>
        <el-table-column label="作者" min-width="180">
          <template #default="{ row }">
            <div class="author-cell">
              <div class="author-avatar">{{ row.name.charAt(0).toUpperCase() }}</div>
              <div>
                <div class="author-name">{{ row.name }}</div>
                <div class="author-email" v-if="row.email">{{ row.email }}</div>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="提交数" prop="commits" width="100" sortable />
        <el-table-column label="新增行" width="110" sortable sort-by="additions">
          <template #default="{ row }">{{ formatNum(row.additions) }}</template>
        </el-table-column>
        <el-table-column label="删除行" width="110" sortable sort-by="deletions">
          <template #default="{ row }">{{ formatNum(row.deletions) }}</template>
        </el-table-column>
        <el-table-column label="仓库数" prop="repo_count" width="80" align="center" sortable />
        <el-table-column label="最近提交" width="120">
          <template #default="{ row }">
            <span v-if="row.last_date">{{ formatDate(row.last_date) }}</span>
            <span v-else class="text-muted">-</span>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="filteredAuthors.length === 0" class="empty-state">暂无数据，请先在统计分析页刷新采集</div>
    </div>
  </div>
</template>

<script>
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api'
import { ArrowLeft } from '@element-plus/icons-vue'

export default {
  components: { ArrowLeft },
  setup() {
    const route = useRoute()
    const router = useRouter()
    const serverId = ref(route.query.server_id || '')
    const authors = ref([])
    const searchText = ref('')
    const sortBy = ref('commits')

    const filteredAuthors = computed(() => {
      if (!searchText.value) return authors.value
      const q = searchText.value.toLowerCase()
      return authors.value.filter(a =>
        a.name.toLowerCase().includes(q) || (a.email && a.email.toLowerCase().includes(q))
      )
    })

    function loadData() {
      if (!serverId.value) return
      api.get(`/statistics/${serverId.value}/authors?sort_by=${sortBy.value}&limit=100`).then(res => {
        authors.value = res.data
      }).catch(() => {})
    }

    function goDetail(row) {
      router.push({ path: `/statistics/authors/${encodeURIComponent(row.name)}`, query: { server_id: serverId.value } })
    }

    function goBack() {
      router.push('/statistics')
    }

    function formatNum(n) {
      if (!n) return '0'
      if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M'
      if (n >= 1000) return (n / 1000).toFixed(1) + 'K'
      return String(n)
    }

    function formatDate(iso) {
      if (!iso) return ''
      return iso.slice(0, 10)
    }

    onMounted(() => { loadData() })

    return { serverId, authors, filteredAuthors, searchText, sortBy, loadData, goDetail, goBack, formatNum, formatDate }
  },
}
</script>

<style scoped>
.section-header {
  display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px;
}
.header-left { display: flex; align-items: center; gap: 8px; }
.section-title { font-size: 18px; font-weight: 700; color: #1a1a2e; margin: 0; }
.header-actions { display: flex; align-items: center; gap: 8px; }

.author-cell { display: flex; align-items: center; gap: 10px; }
.author-avatar {
  width: 32px; height: 32px; border-radius: 50%; flex-shrink: 0;
  background: linear-gradient(135deg, #667eea, #764ba2); color: #fff;
  display: flex; align-items: center; justify-content: center;
  font-size: 14px; font-weight: 700;
}
.author-name { font-size: 13px; font-weight: 600; color: #1a1a2e; cursor: pointer; }
.author-email { font-size: 11px; color: #9ca3af; }

.rank-badge {
  display: inline-flex; align-items: center; justify-content: center;
  width: 22px; height: 22px; border-radius: 5px; font-size: 11px; font-weight: 700;
  background: rgba(0,0,0,0.04); color: #6b7280;
}
.rank-top { background: linear-gradient(135deg, #667eea, #764ba2); color: #fff; }

.text-muted { color: #d1d5db; }
.empty-state { text-align: center; padding: 40px; color: #9ca3af; font-size: 13px; }

:deep(.el-table) { --el-table-border-color: rgba(0,0,0,0.04); }
:deep(.el-table__row) { cursor: pointer; }
</style>
