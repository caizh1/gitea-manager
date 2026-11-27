<template>
  <div class="scope">
    <div class="scope-toolbar">
      <el-input v-model="query" clearable placeholder="按文件路径搜索" aria-label="按文件路径搜索" />
      <el-radio-group v-model="filter" size="small" aria-label="筛选文件范围">
        <el-radio-button value="all">全部 {{ files.length }}</el-radio-button>
        <el-radio-button value="included">{{ completed ? '已扫描' : '候选' }} {{ included }}</el-radio-button>
        <el-radio-button value="excluded">排除 {{ files.length - included }}</el-radio-button>
      </el-radio-group>
    </div>
    <el-table :data="pageRows" row-key="path" size="small" border class="scope-table" empty-text="没有匹配的文件">
      <el-table-column label="文件路径" min-width="170">
        <template #default="{ row }"><code>{{ row.path }}</code></template>
      </el-table-column>
      <el-table-column label="扫描状态" width="95">
        <template #default="{ row }">
          <el-tag :type="row.will_review ? 'success' : 'info'" size="small" round disable-transitions>
            {{ row.will_review ? (completed ? '已扫描' : '将扫描') : '已排除' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="排除原因" min-width="138">
        <template #default="{ row }">{{ row.will_review ? '—' : (reasons[row.exclude_reason] || row.exclude_reason || 'OCR 排除') }}</template>
      </el-table-column>
    </el-table>
    <el-pagination v-if="matching.length > pageSize" v-model:current-page="page"
                   :page-size="pageSize" :total="matching.length" layout="total, prev, pager, next" :pager-count="5" small />
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'

const props = defineProps({
  files: { type: Array, default: () => [] },
  completed: Boolean,
})

const query = ref('')
const filter = ref('all')
const page = ref(1)
const pageSize = 8
const reasons = {
  unsupported_ext: '文件类型未纳入',
  secret_exclude: '命中敏感文件排除规则',
  too_large: '文件大小或上下文超限',
  gitignore: '命中 Git 忽略规则',
  excluded: '命中 OCR 排除规则',
}
const included = computed(() => props.files.filter(file => file.will_review).length)
const matching = computed(() => {
  const text = query.value.trim().toLowerCase()
  return props.files.filter(file => (filter.value === 'all' || file.will_review === (filter.value === 'included')) &&
    file.path.toLowerCase().includes(text))
})
const pageRows = computed(() => matching.value.slice((page.value - 1) * pageSize, page.value * pageSize))
watch([query, filter, () => props.files.length], () => { page.value = 1 })
</script>

<style scoped>
.scope { display: grid; gap: 10px; min-width: 0; }
.scope-toolbar { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
.scope-toolbar .el-input { width: min(100%, 360px); }
.scope-table { width: 100%; border-radius: 8px; --el-table-header-bg-color: #eef3fa; }
.scope-table code { overflow-wrap: anywhere; color: var(--el-text-color-primary); }
.scope .el-pagination { justify-content: flex-end; flex-wrap: wrap; }
</style>
