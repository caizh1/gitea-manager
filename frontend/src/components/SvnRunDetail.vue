<template>
  <section v-if="run" class="svn-detail">
    <SvnSteps :active="3" />
    <el-button link type="primary" @click="$emit('back')"><el-icon><Back /></el-icon>返回执行记录</el-button>
    <div class="page-heading"><div><div class="title-line"><h2>{{ run.snapshot?.plan_name || '迁移任务 #'+run.id }}</h2><el-tag :type="tone(run.status)">{{ run.status_label }}</el-tag></div><p>任务 #{{ run.id }} · {{ targetName }} · 全局串行 · 已完成 {{ run.completed }} / {{ run.total }} 个仓库</p></div><div class="button-group"><el-button v-if="['queued','running'].includes(run.status)" :disabled="busy || !!run.control" @click="$emit('control','pause')">完成当前仓库后暂停</el-button><el-button v-if="['paused','failed','partial','cancelled','uncertain'].includes(run.status)" type="primary" :loading="busy" @click="$emit('control',run.status==='uncertain'?'reconcile':'resume')">{{ run.status==='uncertain' ? '核对远端结果' : '核对并恢复' }}</el-button><el-button v-if="['queued','running','paused','uncertain'].includes(run.status)" type="danger" plain :disabled="busy || run.control==='cancel'" @click="$emit('control','cancel')">取消迁移</el-button><el-button link type="primary" @click="$emit('report')"><el-icon><Download /></el-icon>下载报告</el-button></div></div>
    <el-alert v-if="run.error" :title="run.error" :type="run.status==='uncertain'?'warning':'error'" :closable="false" />
    <el-alert v-if="run.control" :title="run.control==='pause' ? '已请求暂停，当前仓库完成后生效；不会启动下一个仓库。' : '正在取消当前进程，随后核对远端结果；请保留现场。'" type="info" :closable="false" />
    <div class="execution-grid"><aside class="surface queue"><h3>迁移顺序</h3><p class="muted">每次处理一个仓库；按仓库数统计完成量。</p><template v-for="group in groups" :key="group.id"><h4>{{ group.name }} · {{ group.items.length }}</h4><button v-for="item in group.items" :key="item.id" class="queue-item" :class="{selected:item.id===selectedId}" @click="selectedId=item.id"><el-icon :class="'state-'+tone(item.status)"><CircleCheck v-if="item.status==='success'"/><Loading v-else-if="item.status==='running'"/><Clock v-else-if="item.status==='pending'"/><Warning v-else/></el-icon><span>{{ spec(item).name || '仓库 #'+item.repository_id }}</span><small>{{ item.status_label }}</small></button></template></aside>
      <div v-if="selected" class="surface run-inspector"><div class="title-line"><h3>{{ selectedSpec.name || '仓库 #'+selected.repository_id }}</h3><el-tag type="info"><el-icon><Lock /></el-icon>私有目标</el-tag><el-tag :type="tone(selected.status)">{{ selected.status_label }}</el-tag></div><p class="muted">来源：{{ selectedSpec.source_name || '历史来源' }}<span v-if="selectedSpec.revision"> · 固定修订 r{{ selectedSpec.revision }}</span><span v-if="selectedSpec.owner"> · 目标：{{ selectedSpec.owner }}/{{ selectedSpec.name }}</span></p>
        <ol class="stage-track"><li v-for="([key,label],index) in stageList" :key="key" :class="{done:step>index,active:step===index}"><el-icon><CircleCheck v-if="step>index"/><Loading v-else-if="step===index && selected.status==='running'"/><Warning v-else-if="step===index && ['failed','uncertain','cancelled'].includes(selected.status)"/><Clock v-else/></el-icon><span>{{ label }}</span></li></ol>
        <div class="stage-current"><h3>{{ selected.detail || (selected.status==='pending' ? '等待前面的仓库完成' : selected.status_label) }}</h3><template v-if="progress"><p>已验证 {{ progress.done }} / {{ progress.total }} 个提交</p><el-progress :percentage="progress.percent" :show-text="false" /></template><p v-else class="muted">{{ selected.status==='success' ? '内容、历史与远端回读验证均已通过。' : '仅展示实际阶段；当前阶段未提供可测量进度时，不估算百分比或剩余时间。' }}</p><p class="muted">最近活动：{{ dateTime(run.updated_at) }}</p></div>
        <el-tabs v-model="tab"><el-tab-pane label="活动记录" name="events"><div class="activity-log"><p v-for="entry in visibleEvents" :key="entry.id"><time>{{ dateTime(entry.created_at) }}</time><span>{{ entry.message }}</span></p><el-empty v-if="!visibleEvents.length" description="尚无当前仓库的活动记录" :image-size="50" /></div></el-tab-pane><el-tab-pane label="验证结果" name="results"><template v-if="selected.status==='success'"><p class="notice"><el-icon><CircleCheck /></el-icon>迁移及全量内容验证完成。</p><p v-if="selected.result?.['已验证提交数']">已验证 {{ selected.result['已验证提交数'] }} 个提交</p><a v-if="safeTarget" :href="safeTarget" target="_blank" rel="noopener noreferrer">打开目标仓库</a><details><summary>查看验证摘要</summary><pre>{{ JSON.stringify(selected.result,null,2) }}</pre></details></template><el-empty v-else description="尚未完成全部检查，不能据此判定成功" :image-size="60" /></el-tab-pane><el-tab-pane label="执行快照" name="snapshot"><dl class="facts"><dt>提交时间</dt><dd>{{ dateTime(run.created_at) }}</dd><dt>发起人</dt><dd>{{ run.actor_name }}</dd><dt>关联预检</dt><dd>{{ run.snapshot?.preflight_id ? '#'+run.snapshot.preflight_id : '旧版执行快照' }}</dd><dt>停写确认</dt><dd>{{ run.snapshot?.freeze?.confirmed ? '已记录 · '+dateTime(run.snapshot.freeze.at) : '未记录' }}</dd><dt>完整读取确认</dt><dd>{{ run.snapshot?.full_read_confirmed ? '已记录' : '未记录' }}</dd><dt>作者映射</dt><dd>提交时已固定，恢复不会改写</dd><dt>源仓库 UUID</dt><dd>{{ selectedSpec.uuid || '未记录' }}</dd></dl></el-tab-pane></el-tabs>
        <p class="notice"><el-icon><InfoFilled /></el-icon>推送后还会从 Gitea 回读验证，全部检查通过才标记成功。</p>
      </div>
    </div><div class="toolbar"><p class="footnote">离开页面后，后台继续执行。暂停在当前仓库完成后生效；取消保留已创建资源。</p><el-button v-if="run.status==='success'" @click="$emit('cleanup')">清理成功任务的本地工作区</el-button></div>
  </section>
</template>
<script setup>
import { computed, ref, watch } from 'vue'
import SvnSteps from './SvnSteps.vue'
import { Back, Download, Lock, CircleCheck, Loading, Clock, Warning, InfoFilled } from '@element-plus/icons-vue'
import { stageList, stageIndex, measuredProgress, dateTime, tone } from './svnMigrationDisplay.js'
const props=defineProps({run:Object,events:Array,targetName:String,busy:Boolean})
defineEmits(['back','control','report','cleanup'])
const selectedId=ref(null),tab=ref('events')
const spec=item=>props.run?.snapshot?.repositories?.find(r=>r.repository_id===item.repository_id)||{}
watch(()=>props.run?.id,()=>{selectedId.value=null})
const selected=computed(()=>props.run?.items?.find(i=>i.id===selectedId.value)||props.run?.items?.find(i=>i.status==='running')||props.run?.items?.find(i=>i.status!=='success')||props.run?.items?.[0])
const selectedSpec=computed(()=>selected.value ? spec(selected.value) : {})
// 仅合并相邻来源，跨来源交错的执行顺序不能被分组重排。
const groups=computed(()=>{const groups=[];for(const item of props.run?.items||[]){const r=spec(item),sourceId=r.source_id||0;let group=groups.at(-1);if(!group||group.sourceId!==sourceId){group={id:groups.length,sourceId,name:r.source_name||'历史来源',items:[]};groups.push(group)}group.items.push(item)}return groups})
const step=computed(()=>stageIndex(selected.value)),progress=computed(()=>measuredProgress(selected.value))
const visibleEvents=computed(()=>(props.events||[]).filter(e=>!e.item_id||e.item_id===selected.value?.id))
const safeTarget=computed(()=>{try{const url=new URL(selected.value?.result?.['目标']);return ['http:','https:'].includes(url.protocol)?url.href:null}catch{return null}})
</script>
