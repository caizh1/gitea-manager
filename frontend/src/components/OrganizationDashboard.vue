<template>
  <section class="organization-dashboard"><h2>{{ currentOrganization?.organization_name }}</h2><p>{{ currentOrganization?.server_name }} · 当前组织管理工作台</p><div class="organization-actions"><router-link v-for="item in entries.filter(x => hasCapability(x.capability))" :key="item.path" :to="item.path" class="glass-card organization-entry"><el-icon><component :is="item.icon" /></el-icon><div><strong>{{ item.title }}</strong><p>{{ item.description }}</p></div><el-icon><ArrowRight /></el-icon></router-link></div></section>
</template>
<script setup>
import { Key, CircleCheck, DataAnalysis, ArrowRight } from '@element-plus/icons-vue'
import { currentOrganization, hasCapability } from '../composables/accessState.js'
const entries = [
  { path: '/repository-policies', capability: 'repository_policies', title: '仓库策略', description: '模板、预览与批量应用记录', icon: Key },
  { path: '/commit-gates', capability: 'commit_gates', title: '提交门禁', description: '提交规则及仓库应用状态', icon: Key },
  { path: '/approval-rules', capability: 'approval_rules', title: '审批规则', description: '组织仓库的规则与审批状态', icon: CircleCheck },
  { path: '/ai-reviews', capability: 'ai_reviews', title: 'AI 审查', description: '已接入仓库的任务与经验数据集', icon: DataAnalysis },
  { path: '/statistics', capability: 'statistics', title: '统计分析', description: '当前组织仓库与作者统计', icon: DataAnalysis },
]
</script>
<style scoped>
.organization-dashboard > p { color: var(--text-secondary); }
.organization-actions { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr)); gap: 18px; margin-top: 24px; }
.organization-entry { display: flex; align-items: center; gap: 16px; padding: 24px; text-decoration: none; color: var(--text-primary); min-width: 0; }
.organization-entry > div { flex: 1; min-width: 0; }
.organization-entry p { color: var(--text-secondary); font-size: 13px; line-height: 1.6; }
.organization-entry > .el-icon { flex-shrink: 0; }
</style>
