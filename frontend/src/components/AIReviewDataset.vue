<template>
  <div class="glass-card dataset-panel">
    <div class="dataset-toolbar">
      <el-select v-model="repository" filterable placeholder="选择审查仓库" aria-label="数据集仓库" @change="page = 1; load()"><el-option v-for="r in repositories" :key="r.id" :value="r.id" :label="r.repo_name" /></el-select>
      <el-select v-model="status" clearable placeholder="全部状态" aria-label="样本状态" @change="page = 1; load()"><el-option v-for="(label, key) in states" :key="key" :value="key" :label="label" /></el-select>
      <el-button :icon="Refresh" :loading="busy" :disabled="!repository" @click="load">刷新</el-button>
      <el-button :disabled="!repository" @click="download(false)">导出生效样本</el-button>
      <el-button :disabled="!repository" @click="download(true)">导出历史修订</el-button>
    </div>
    <p>人工评论先收录为候选。具有仓库写权限的开发者确认后，供本仓库后续相关审查参考；不自动训练模型，也不自动认定相似代码有错。</p>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-alert v-if="syncError" :title="'反馈同步：' + syncError" type="warning" :closable="false" />
    <p v-if="pending">还有 {{ pending }} 个评论事件等待核对。</p>
    <details class="commands"><summary>在 PR 对话中反馈或收录</summary><pre>/ai-review 反馈 AR-意见编号 误报 原因
/ai-review 反馈 AR-意见编号 排除 原因 条件=适用范围
/ai-review 反馈 AR-意见编号 部分错误 纠正内容
/ai-review 反馈 AR-意见编号 定位错误 正确位置和原因
/ai-review 反馈 AR-意见编号 有效
/ai-review 反馈 AR-意见编号 撤销
/ai-review 收录 评论链接 触发条件、原因和影响</pre><p>普通评论缺少历史版本时，补充「版本=完整SHA 文件=仓库路径」；路径含空格可用双引号包裹。可选「修复=完整SHA」。不会自动认定修复通过。撤销人工样本使用「/ai-review 反馈 DS-样本编号 撤销」。</p></details>
    <el-table :data="items" stripe>
      <el-table-column label="原始意见" min-width="220"><template #default="{ row }"><strong>{{ row.original?.title }}</strong><p class="muted">{{ row.path || '尚无固定文件' }}</p><small>{{ row.id }} · 修订 {{ row.revision }}</small></template></el-table-column>
      <el-table-column label="人工标注" min-width="240"><template #default="{ row }"><el-tag>{{ labels[row.label] || row.label }}</el-tag><p>{{ row.correction || '尚未确认' }}</p></template></el-table-column>
      <el-table-column label="状态" min-width="145"><template #default="{ row }"><el-tag :type="row.conflict ? 'warning' : row.status === 'active' ? 'success' : 'info'">{{ row.conflict ? '存在冲突，暂停参考' : row.status === 'active' && !row.reference_ready ? '来源待核验，暂停参考' : states[row.status] || row.status }}</el-tag></template></el-table-column>
      <el-table-column label="操作" width="150"><template #default="{ row }"><div class="dataset-toolbar"><el-button link @click="open(row)">原文与修订</el-button><el-button link type="warning" :disabled="row.status === 'disabled'" @click="disable(row)">停用</el-button></div></template></el-table-column>
    </el-table>
    <el-pagination v-if="repository" v-model:current-page="page" :total="total" :page-size="30" layout="prev, pager, next, total" @current-change="load" />
    <el-drawer v-model="visible" title="样本原文与不可变修订" size="min(820px, 96vw)"><article v-for="r in history" :key="r.revision"><h3>修订 {{ r.revision }} · {{ states[r.status] }}</h3><p>确认者 ID：{{ r.confirmed_by || '尚无' }}；PR #{{ r.pr }}；版本：{{ r.head || '未知' }}</p><p>{{ r.source_url }}</p><h4>原始意见</h4><pre>{{ r.original?.explanation }}</pre><h4>人工纠正与适用条件</h4><pre>{{ r.correction || '尚无' }}</pre><p v-if="r.invalidation">失效原因：{{ r.invalidation }}</p><details><summary>固定版本上下文</summary><pre>{{ JSON.stringify(r.context, null, 2) }}</pre></details></article></el-drawer>
  </div>
</template>
<script setup>
import { ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { api } from '../api'
defineProps({ repositories: { type: Array, default: () => [] } })
const repository = ref(null), status = ref(''), items = ref([]), page = ref(1), total = ref(0), busy = ref(false), error = ref(''), pending = ref(0), syncError = ref(''), history = ref([]), visible = ref(false)
const states = { candidate: '待开发者确认', pending: '待补充信息', active: '已确认', withdrawn: '已撤销或失效', disabled: '管理员停用' }
const labels = { unconfirmed: '人工候选', confirmed_defect: '人工确认缺陷', false_positive: '误报', exclude_by_policy: '明确排除', partial_error: '部分错误', wrong_location: '定位错误', valid: '有效', withdrawn: '撤销' }
let generation = 0
const path = () => `/ai-review/repositories/${repository.value}/dataset`
const fail = e => { error.value = e.response?.data?.error || '数据集操作失败，请检查服务状态' }
async function load() { const current = ++generation; if (!repository.value) { items.value = []; return } busy.value = true; error.value = ''; try { const r = await api.get(path(), { params: { page: page.value, status: status.value || undefined } }); if (current !== generation) return; items.value = r.data.items; total.value = r.data.total; pending.value = r.data.pending_events; syncError.value = r.data.sync_error } catch (e) { if (current === generation) fail(e) } finally { if (current === generation) busy.value = false } }
async function open(row) { try { history.value = (await api.get(path() + `/${row.id}/history`)).data; visible.value = true } catch (e) { fail(e) } }
async function disable(row) { const target = path(); try { await ElMessageBox.confirm('停用后不再提供给新审查任务，原始意见和历史修订保留。', '停用样本', { confirmButtonText: '停用', cancelButtonText: '取消' }); const csrf = (await api.get('/ai-review/csrf')).data.token; await api.post(target + `/${row.id}/disable`, {}, { headers: { 'X-Review-CSRF': csrf } }); ElMessage.success('样本已停用'); await load() } catch (e) { if (e !== 'cancel' && e !== 'close') fail(e) } }
async function download(all) { try { const r = await api.get(path() + '/export', { params: { history: all ? '1' : '0' }, responseType: 'blob' }); const url = URL.createObjectURL(r.data); const link = document.createElement('a'); link.href = url; link.download = `审查数据集-${repository.value}-${all ? '历史' : '生效'}.zip`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000) } catch (e) { fail(e) } }
</script>
<style scoped>
.dataset-panel { padding: 24px; min-width: 0; overflow-wrap: anywhere; }
.dataset-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; min-width: 0; }
.dataset-toolbar .el-select { width: 260px; max-width: 100%; }
p { line-height: 1.7; } .muted { color: var(--text-secondary); } .commands { margin: 18px 0; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; padding: 12px; border-radius: 8px; background: #f3f6fa; }
article { border-bottom: 1px solid #dfe6ed; padding: 16px 0; }
.el-pagination { margin-top: 20px; flex-wrap: wrap; }
@media (max-width: 720px) { .dataset-panel { padding: 14px; } }
</style>
