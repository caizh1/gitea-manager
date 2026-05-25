<template>
  <div>
    <div class="section-header">
      <h3 class="section-title">提交门禁</h3>
    </div>

    <div class="glass-card gate-toolbar">
      <el-select v-model="selectedServerId" placeholder="选择 Gitea 服务器" class="server-select" @change="onServerChange">
        <el-option v-for="s in servers" :key="s.id" :label="`${s.name} (${s.host})`" :value="s.id" />
      </el-select>
      <el-button :icon="Refresh" :disabled="!selectedServerId" @click="reloadAll">刷新</el-button>
    </div>

    <div class="gate-grid">
      <div class="glass-card gate-panel">
        <div class="card-section-header">
          <span class="card-section-title">规则模板</span>
          <el-button size="small" :icon="Plus" @click="newRule">新建</el-button>
        </div>

        <el-empty v-if="!selectedServerId" description="请选择服务器" />
        <template v-else>
          <el-table :data="rules" size="small" stripe class="rules-table" @row-click="editRule">
            <el-table-column prop="name" label="规则" min-width="130" />
            <el-table-column label="状态" width="72">
              <template #default="{ row }">
                <el-tag :type="row.enabled ? 'success' : 'info'" size="small">{{ row.enabled ? '启用' : '停用' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="assignment_count" label="仓库" width="70" />
            <el-table-column label="操作" width="92">
              <template #default="{ row }">
                <el-button text size="small" :icon="Edit" @click.stop="editRule(row)" />
                <el-button text size="small" type="danger" :icon="Delete" @click.stop="deleteRule(row)" />
              </template>
            </el-table-column>
          </el-table>

          <el-form label-width="84px" class="rule-form">
            <el-form-item label="规则名称">
              <el-input v-model="ruleForm.name" placeholder="Conventional Commits" />
            </el-form-item>
            <el-form-item label="正则">
              <el-input v-model="ruleForm.pattern" type="textarea" :rows="3" placeholder="grep -E 正则" />
            </el-form-item>
            <el-form-item label="拒绝提示">
              <el-input v-model="ruleForm.reject_message" type="textarea" :rows="2" />
            </el-form-item>
            <el-form-item label="启用">
              <el-switch v-model="ruleForm.enabled" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :icon="Check" :loading="savingRule" @click="saveRule">{{ editingRuleId ? '保存' : '创建' }}</el-button>
              <el-button @click="loadDefaultRule">默认模板</el-button>
            </el-form-item>
          </el-form>

          <div class="test-box">
             <el-input v-model="testMessage" placeholder="[ID-123] fix: repair restore token sync" />
            <el-button :icon="Finished" @click="testRule">测试</el-button>
            <el-tag v-if="testResult" :type="testResult.matched ? 'success' : 'danger'" size="small">
              {{ testResult.matched ? '通过' : '不通过' }}
            </el-tag>
          </div>
        </template>
      </div>

      <div class="glass-card gate-panel repo-panel">
        <div class="card-section-header">
          <span class="card-section-title">仓库应用</span>
          <div class="repo-actions">
            <el-select v-model="selectedRuleId" placeholder="选择规则" size="small" class="rule-select">
              <el-option v-for="r in enabledRules" :key="r.id" :label="r.name" :value="r.id" />
            </el-select>
            <el-button size="small" type="primary" :icon="Check" :disabled="!canApplySelected" :loading="applying" @click="applySelected">应用所选</el-button>
            <el-button size="small" :icon="SetUp" :disabled="!selectedRuleId" :loading="applying" @click="applyAll">应用全部</el-button>
            <el-button size="small" type="danger" :icon="Close" :disabled="!selectedRepoNames.length" :loading="removing" @click="removeSelected">移除所选</el-button>
          </div>
        </div>

        <el-table
          :data="repos"
          v-loading="loadingRepos"
          stripe
          height="520"
          @selection-change="onRepoSelection"
        >
          <el-table-column type="selection" width="44" />
          <el-table-column prop="repo_name" label="仓库" min-width="220" />
          <el-table-column prop="rule_name" label="当前规则" min-width="150">
            <template #default="{ row }">{{ row.rule_name || '-' }}</template>
          </el-table-column>
          <el-table-column label="安装状态" width="100">
            <template #default="{ row }">
              <el-tag v-if="row.install_status" :type="row.install_status === 'success' ? 'success' : row.install_status === 'failed' ? 'danger' : 'warning'" size="small">
                {{ row.install_status }}
              </el-tag>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column prop="applied_at" label="应用时间" width="170">
            <template #default="{ row }">{{ fmt(row.applied_at) }}</template>
          </el-table-column>
          <el-table-column prop="install_log" label="日志" min-width="180" show-overflow-tooltip />
        </el-table>
      </div>
    </div>
  </div>
</template>

<script>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Check, Close, Delete, Edit, Finished, Plus, Refresh, SetUp } from '@element-plus/icons-vue'
import { api } from '../api'

export default {
  setup() {
    const servers = ref([])
    const selectedServerId = ref(null)
    const rules = ref([])
    const repos = ref([])
    const defaultRule = ref(null)
    const selectedRepoNames = ref([])
    const selectedRuleId = ref(null)
    const editingRuleId = ref(null)
    const savingRule = ref(false)
    const loadingRepos = ref(false)
    const applying = ref(false)
    const removing = ref(false)
    const testMessage = ref('[ID-123] fix: repair restore token sync')
    const testResult = ref(null)
    const ruleForm = ref({ name: '', pattern: '', reject_message: '', enabled: true })

    const enabledRules = computed(() => rules.value.filter(r => r.enabled))
    const canApplySelected = computed(() => selectedRuleId.value && selectedRepoNames.value.length > 0)

    function loadServers() {
      return api.get('/servers').then(res => {
        servers.value = res.data
        if (!selectedServerId.value && servers.value.length) {
          selectedServerId.value = servers.value[0].id
        }
      })
    }

    function loadRules() {
      if (!selectedServerId.value) return Promise.resolve()
      return api.get(`/commit-rules?server_id=${selectedServerId.value}`).then(res => {
        rules.value = res.data.rules || []
        defaultRule.value = res.data.default_rule
        if (!selectedRuleId.value && enabledRules.value.length) {
          selectedRuleId.value = enabledRules.value[0].id
        }
        if (!editingRuleId.value && !ruleForm.value.pattern) loadDefaultRule()
      })
    }

    function loadRepos() {
      if (!selectedServerId.value) return Promise.resolve()
      loadingRepos.value = true
      return api.get(`/commit-gates/repos?server_id=${selectedServerId.value}`)
        .then(res => { repos.value = res.data })
        .catch(e => ElMessage.error(e.response?.data?.error || '仓库加载失败'))
        .finally(() => { loadingRepos.value = false })
    }

    function reloadAll() {
      return Promise.all([loadRules(), loadRepos()])
    }

    function onServerChange() {
      selectedRuleId.value = null
      editingRuleId.value = null
      selectedRepoNames.value = []
      ruleForm.value = { name: '', pattern: '', reject_message: '', enabled: true }
      reloadAll()
    }

    function loadDefaultRule() {
      const d = defaultRule.value
      if (!d) return
      editingRuleId.value = null
      testResult.value = null
      ruleForm.value = {
        name: d.name,
        pattern: d.pattern,
        reject_message: d.reject_message,
        enabled: true,
      }
    }

    function newRule() {
      editingRuleId.value = null
      testResult.value = null
      ruleForm.value = { name: '', pattern: '', reject_message: '', enabled: true }
      loadDefaultRule()
    }

    function editRule(row) {
      editingRuleId.value = row.id
      testResult.value = null
      ruleForm.value = {
        name: row.name,
        pattern: row.pattern,
        reject_message: row.reject_message,
        enabled: row.enabled,
      }
      selectedRuleId.value = row.id
    }

    function saveRule() {
      if (!selectedServerId.value) return
      savingRule.value = true
      const payload = { ...ruleForm.value, server_id: selectedServerId.value }
      const req = editingRuleId.value
        ? api.put(`/commit-rules/${editingRuleId.value}`, payload)
        : api.post('/commit-rules', payload)
      req.then(res => {
        ElMessage.success(editingRuleId.value ? '规则已保存' : '规则已创建')
        selectedRuleId.value = res.data.id
        editingRuleId.value = res.data.id
        loadRules()
      }).catch(e => {
        ElMessage.error(e.response?.data?.detail || e.response?.data?.error || '保存失败')
      }).finally(() => { savingRule.value = false })
    }

    function deleteRule(row) {
      ElMessageBox.confirm(`删除规则「${row.name}」？`, '确认删除', { type: 'warning' }).then(() => {
        api.delete(`/commit-rules/${row.id}`).then(() => {
          ElMessage.success('规则已删除')
          if (editingRuleId.value === row.id) newRule()
          loadRules()
        }).catch(e => ElMessage.error(e.response?.data?.error || '删除失败'))
      }).catch(() => {})
    }

    function testRule() {
      const pattern = (ruleForm.value.pattern || '').trim()
      const message = (testMessage.value || '').trim()
      if (!pattern) {
        testResult.value = null
        ElMessage.warning('请先填写正则或点击默认模板')
        return
      }
      if (!message) {
        testResult.value = null
        ElMessage.warning('请填写测试提交信息')
        return
      }
      api.post('/commit-gates/test', {
        pattern,
        message,
      }).then(res => {
        if (!res.data.ok) {
          testResult.value = null
          ElMessage.error(res.data.error || '正则无效')
          return
        }
        testResult.value = res.data
      })
    }

    function onRepoSelection(rows) {
      selectedRepoNames.value = rows.map(r => r.repo_name)
    }

    function applySelected() {
      applyGate({ repo_names: selectedRepoNames.value })
    }

    function applyAll() {
      ElMessageBox.confirm('将当前规则应用到该服务器当前全部仓库？', '确认应用', { type: 'warning' })
        .then(() => applyGate({ apply_all: true }))
        .catch(() => {})
    }

    function applyGate(extra) {
      applying.value = true
      api.post('/commit-gates/apply', {
        server_id: selectedServerId.value,
        rule_id: selectedRuleId.value,
        ...extra,
      }).then(res => {
        const failed = (res.data.results || []).filter(r => r.install_status === 'failed')
        if (failed.length) ElMessage.warning(`${failed.length} 个仓库应用失败`)
        else ElMessage.success('门禁已应用')
        loadRepos()
        loadRules()
      }).catch(e => ElMessage.error(e.response?.data?.error || '应用失败'))
        .finally(() => { applying.value = false })
    }

    function removeSelected() {
      removing.value = true
      api.post('/commit-gates/remove', {
        server_id: selectedServerId.value,
        repo_names: selectedRepoNames.value,
      }).then(() => {
        ElMessage.success('门禁已移除')
        loadRepos()
        loadRules()
      }).catch(e => ElMessage.error(e.response?.data?.error || '移除失败'))
        .finally(() => { removing.value = false })
    }

    function fmt(d) { return d ? new Date(d).toLocaleString() : '-' }

    onMounted(() => {
      loadServers().then(() => reloadAll())
    })

    return {
      servers, selectedServerId, rules, repos, selectedRepoNames, selectedRuleId,
      enabledRules, canApplySelected, ruleForm, editingRuleId, savingRule,
      loadingRepos, applying, removing, testMessage, testResult,
      loadDefaultRule, newRule, editRule, saveRule, deleteRule, testRule,
      onRepoSelection, applySelected, applyAll, removeSelected, onServerChange,
      reloadAll, fmt,
      Check, Close, Delete, Edit, Finished, Plus, Refresh, SetUp,
    }
  },
}
</script>

<style scoped>
.section-header { margin-bottom: 18px; }
.section-title { font-size: 18px; font-weight: 700; color: #1a1a2e; margin: 0; }
.gate-toolbar { padding: 16px; margin-bottom: 18px; display: flex; gap: 12px; align-items: center; }
.server-select { width: 360px; max-width: 100%; }
.gate-grid { display: grid; grid-template-columns: minmax(320px, 420px) minmax(0, 1fr); gap: 18px; align-items: start; }
.gate-panel { padding: 20px; }
.repo-panel { min-width: 0; }
.card-section-title { font-weight: 600; font-size: 15px; color: #1a1a2e; }
.card-section-header { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 14px; }
.rules-table { margin-bottom: 16px; }
.rule-form { border-top: 1px solid rgba(0,0,0,0.06); padding-top: 16px; }
.test-box { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 8px; align-items: center; }
.repo-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
.rule-select { width: 180px; }

@media (max-width: 1100px) {
  .gate-grid { grid-template-columns: 1fr; }
  .card-section-header { align-items: flex-start; flex-direction: column; }
  .repo-actions { justify-content: flex-start; }
}
</style>
