<template>
  <section v-if="record" class="svn-detail">
    <SvnSteps :active="2" />
    <el-button link type="primary" @click="$emit('back')"><el-icon><Back /></el-icon>返回上一页</el-button>
    <div class="page-heading"><div><div class="title-line"><h2>预检详情 #{{ record.id }}</h2><el-tag :type="checkTone(record)">{{ checkLabel(record) }}</el-tag></div><p>{{ record.plan_name }} · 检查时间 {{ dateTime(record.created_at) }}</p></div><div class="button-group"><el-button @click="$emit('plan',record.plan_id)">查看关联计划</el-button><el-button type="primary" :loading="busy" :disabled="['queued','preflight'].includes(record.status)" @click="$emit('preflight',record.plan_id)">重新预检</el-button></div></div>
    <p class="notice"><el-icon><InfoFilled /></el-icon>工作台与来源页查看的是同一份预检记录；结果仅适用于本计划及当次输入。</p>
    <el-alert v-if="!record.current && !['queued','preflight'].includes(record.status)" title="这份结果已过期或检查未完成，不能作为当前执行依据。请重新预检。" type="warning" :closable="false" />
    <div class="preflight-grid">
      <div class="surface">
        <h3>阻断问题（{{ record.preview?.issues?.length || 0 }}）</h3>
        <el-empty v-if="!record.preview?.issues?.length" :description="record.status==='ready' ? '本次预检未发现阻断问题' : '检查尚未完成，请等待或重新预检'" />
        <section v-for="(issue,index) in record.preview?.issues || []" :key="index" class="issue-block">
          <div class="title-line"><span class="error">问题 P{{ record.id }}-{{ index+1 }}</span><el-tag type="danger">当次预检受阻</el-tag></div>
          <h3>{{ issue.message }}</h3><p class="muted">{{ repositoryName(issue.repository_id) }}<span v-if="issue.url"> · {{ issue.url }}</span></p>
          <p>影响：{{ issue.code==='externals' ? '本期不支持转换外部引用，包含该范围的仓库不能执行迁移。' : '需要解决此项问题，再重新预检，才能提交执行。' }}</p>
          <p class="muted">这是本计划当次检查的结论，不是仓库的永久迁移状态。</p>
        </section>
        <h3>可采取的操作</h3>
        <div class="action-row"><div><strong>调整计划范围</strong><p class="muted">明确排除受影响仓库，或修改配置后重新生成预览；排除项仍写入报告。</p></div><el-button @click="$emit('edit',record.plan_id)">调整关联计划</el-button></div>
        <div class="action-row"><div><strong>处理来源或目标问题后重新预检</strong><p class="muted">Manager 不会自动修改 SVN 属性、展开外部引用或忽略阻断。</p></div></div>
        <details><summary>查看检查范围与证据</summary><div v-for="repo in evidenceRows" :key="repo.repository_id" class="evidence-row"><strong>{{ repo.source_name }} / {{ repo.name }}</strong><span>{{ repo.url }}</span><span v-if="repo.revision">固定修订 r{{ repo.revision }} · UUID {{ repo.uuid }}</span><span v-if="repo.layout">主干 {{ repo.layout.trunk || '/' }} · 分支 {{ repo.layout.branches || '无' }} · 标签 {{ repo.layout.tags || '无' }}</span></div></details>
      </div>
      <aside class="surface"><h3>本次预检</h3><dl class="facts"><dt>关联计划</dt><dd>{{ record.plan_name }}</dd><dt>预检编号</dt><dd>#{{ record.id }}</dd><dt>检查时间</dt><dd>{{ dateTime(record.created_at) }}</dd><dt>目标实例</dt><dd>{{ targetName }}</dd><dt>计划范围</dt><dd>{{ record.config?.repositories?.length || 0 }} 个仓库</dd><dt>输入状态</dt><dd>{{ record.current ? '与当前计划一致' : '已变化或未完整检查' }}</dd></dl><h3>范围结果</h3><p class="muted">以下是预检结论，尚不代表迁移完成。</p><div v-for="repo in record.config?.repositories || []" :key="repo.id" class="scope-row"><span>{{ repo.name }}</span><el-tag :type="repoState(repo.id)==='通过' ? 'success' : repoState(repo.id)==='受阻' ? 'danger' : 'info'">{{ repoState(repo.id) }}</el-tag></div><p class="muted">原记录只读；重新预检生成新记录，旧记录保留。</p><div class="button-group history-navigation"><el-button :disabled="!record.previous_id" @click="$emit('check',record.previous_id)">上一份预检</el-button><el-button :disabled="!record.next_id" @click="$emit('check',record.next_id)">下一份预检</el-button></div></aside>
    </div>
    <p class="footnote">来源、布局、目标或作者映射变化后需重新预检。历史记录可以查看，但不能替代当前执行预览。</p>
  </section>
</template>
<script setup>
import { computed } from 'vue'
import SvnSteps from './SvnSteps.vue'
import { Back, InfoFilled } from '@element-plus/icons-vue'
import { checkLabel, checkTone, dateTime } from './svnMigrationDisplay.js'
const props=defineProps({record:Object,targetName:String,busy:Boolean})
defineEmits(['back','plan','edit','preflight','check'])
const evidenceRows=computed(()=>(props.record.preview?.selected || props.record.preview?.repositories || []).map(row=>({...row,...props.record.preview?.repositories?.find(r=>r.repository_id===row.repository_id)})))
function repositoryName(id){return props.record.preview?.selected?.find(r=>r.repository_id===id)?.name || props.record.config?.repositories?.find(r=>r.id===id)?.name || '计划范围'}
function repoState(id){const preview=props.record.preview || {};return (preview.issues || []).some(i=>!i.repository_id || i.repository_id===id) ? '受阻' : (preview.repositories || []).some(r=>r.repository_id===id) ? '通过' : '未完成检查'}
</script>
