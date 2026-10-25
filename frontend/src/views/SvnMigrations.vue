<template>
  <div ref="center" class="svn-center">
    <el-alert v-if="pollingError" :title="pollingError" type="warning" :closable="false" />
    <SvnPlanEditor v-if="page==='plan' && stateLoaded" :key="route.fullPath" :plan-id="route.params.planId ? Number(route.params.planId) : null" :repositories="planRepositories" :selected-ids="routeSelectedIds" :targets="state.targets" @back="navigate('sources')" @saved="onPlanSaved" />
    <SvnPreflightDetail v-else-if="page==='preflight' && currentCheck" :record="currentCheck" :target-name="targetName(currentCheck.server_id)" :busy="saving" @back="backFromCheck" @check="openCheck" @plan="showPlan" @edit="editPlanById" @preflight="preflightById" />
    <SvnRunDetail v-else-if="page==='run' && currentRun" :run="currentRun" :events="runEvents" :target-name="targetName(currentRun.server_id)" :busy="saving" @back="navigate('runs')" @control="controlRun" @report="downloadReport" @cleanup="cleanupRun" />
    <template v-else-if="['preflight','run','plan'].includes(page)"><el-skeleton v-if="!detailError" :rows="8" animated /><el-result v-else icon="warning" title="无法读取详情" :sub-title="detailError"><template #extra><el-button @click="refresh">重试</el-button><el-button @click="navigate('workbench')">返回迁移工作台</el-button></template></el-result></template>
    <template v-else>
      <div class="page-heading"><div><h2>{{ page==='sources'?'来源与仓库':page==='runs'?'执行记录':'迁移工作台' }}</h2><p>{{ page==='sources'?'管理来源与仓库清单，跨来源选择后创建迁移计划。':page==='runs'?'每次提交计划形成独立任务；中断恢复继续原任务。':'统一规划多个 SVN，跟踪计划进度与执行前待办。' }}</p></div><div class="button-group"><el-button v-if="page==='sources'" @click="newSource"><el-icon><Plus /></el-icon>添加 SVN 来源</el-button><template v-else-if="page==='workbench'"><el-button @click="navigate('sources')">管理来源与仓库</el-button><el-button type="primary" @click="navigate('sources')">新建迁移计划</el-button></template><el-button link :loading="refreshing" @click="refresh"><el-icon><Refresh /></el-icon>刷新</el-button></div></div>
      <template v-if="page==='workbench'">
        <div class="overview-line"><span><el-icon><InfoFilled /></el-icon>{{ state.sources.length }} 个 SVN 来源 · {{ state.repositories.length }} 个已登记仓库</span><el-tag :type="state.health?.worker?.online?'success':'warning'">{{ state.health?.worker?.online?'执行器在线':'执行器离线，任务将等待' }}</el-tag></div>
        <section class="surface"><div class="section-heading"><h3>迁移计划</h3><el-select v-model="planFilter" aria-label="计划状态筛选" placeholder="全部状态" class="filter-select"><el-option label="全部状态" value=""/><el-option label="预检受阻" value="blocked"/><el-option label="需重新预检" value="stale"/><el-option label="可执行" value="ready"/><el-option label="执行中" value="running"/></el-select></div>
          <el-table class="plan-table" :data="visiblePlans" row-key="id"><el-table-column label="计划" min-width="170"><template #default="{row}"><strong>{{ row.name }}</strong><small class="muted block">计划 #{{ row.id }}</small></template></el-table-column><el-table-column label="范围 / 目标" min-width="170"><template #default="{row}">{{ planSourceCount(row) }} 个来源 · {{ row.config.repositories.length }} 个仓库<small class="muted block">{{ targetName(row.server_id) }}</small></template></el-table-column><el-table-column label="状态" min-width="130"><template #default="{row}"><el-tag :type="planState(row).type">{{ planState(row).label }}</el-tag></template></el-table-column><el-table-column label="最近预检" min-width="170"><template #default="{row}"><el-button v-if="row.preflight" link type="primary" @click="openCheck(row.preflight.id)">#{{ row.preflight.id }} · {{ dateTime(row.preflight.created_at) }}</el-button><span v-else class="muted">{{ row.preview?.created_at?'旧版预览':'尚未预检' }}</span><small v-if="row.preflight && !row.preflight.current && !['queued','preflight'].includes(row.preflight.status)" class="muted block">已过期或未完成检查</small></template></el-table-column><el-table-column label="操作" min-width="200"><template #default="{row}"><div class="button-group"><el-button v-if="latestRun(row) && latestRun(row).plan_version===row.version && latestRun(row).preflight_id===row.preflight?.id" @click="openRun(latestRun(row).id)">查看执行</el-button><el-button v-else @click="actOnPlan(row)">{{ planActionLabel(row) }}</el-button><el-dropdown trigger="click"><el-button aria-label="更多计划操作"><el-icon><MoreFilled /></el-icon></el-button><template #dropdown><el-dropdown-menu><el-dropdown-item :disabled="['queued','preflight'].includes(row.status)" @click="editPlan(row)">编辑计划</el-dropdown-item><el-dropdown-item :disabled="['queued','preflight'].includes(row.status)" @click="preflight(row)">重新预检</el-dropdown-item><el-dropdown-item v-if="row.preflight" @click="openCheck(row.preflight.id)">查看预检详情</el-dropdown-item></el-dropdown-menu></template></el-dropdown></div></template></el-table-column><template #empty><el-empty description="尚无迁移计划，先登记来源并选择仓库" :image-size="70"><el-button type="primary" @click="navigate('sources')">选择仓库</el-button></el-empty></template></el-table>
        </section>
        <section class="surface"><div class="section-heading"><div><h3>计划待办</h3><p class="muted">按计划处理；来源页仅引用同一预检记录。</p></div><el-tag v-if="blockerCount" type="danger">{{ blockerCount }} 条有效阻断</el-tag></div><div v-for="todo in todos" :key="todo.key" class="todo-row"><el-icon :class="'state-'+todo.type"><Warning v-if="todo.type==='danger'"/><InfoFilled v-else/></el-icon><div class="todo-plan"><strong>{{ todo.plan.name }}</strong><small v-if="todo.repository" class="muted">{{ todo.repository }}</small></div><div class="todo-reason"><span>{{ todo.message }}</span><small class="muted">{{ todo.check ? '预检 #'+todo.check.id+' · '+dateTime(todo.check.created_at) : '计划执行前待办' }}</small></div><el-button @click="todo.check && todo.type==='danger'?openCheck(todo.check.id):todo.type==='info'?preflight(todo.plan):openPlan(todo.plan)">{{ todo.type==='danger'?'查看预检详情':todo.type==='info'?'重新预检':'查看计划' }}</el-button></div><el-empty v-if="!todos.length" description="当前没有待处理事项" :image-size="55" /></section>
        <p class="footnote">一行代表一个迁移计划，可包含多个来源的多个仓库；每次提交产生独立执行任务。迁入仓库保持私有。</p>
      </template>
      <template v-else-if="page==='sources'">
        <el-empty v-if="!state.sources.length" description="添加 SVN 实例，自动发现并选择项目"><el-button type="primary" @click="newSource">添加 SVN 来源</el-button></el-empty>
        <div v-else class="source-grid"><aside class="surface source-list"><h3>SVN 来源</h3><el-input v-model="sourceSearch" placeholder="搜索来源名称" clearable aria-label="搜索来源名称"/><button class="source-choice" :class="{selected:sourceId===null}" @click="sourceId=null"><el-icon><FolderOpened /></el-icon><span>全部来源</span><small>{{ state.sources.length }}</small></button><button v-for="source in filteredSources" :key="source.id" class="source-choice" :class="{selected:sourceId===source.id}" @click="sourceId=source.id"><el-icon><FolderOpened /></el-icon><span>{{ source.name }}<small v-if="source.archived" class="muted">（已停用）</small></span><small>{{ sourceRepositories(source.id).length }}</small></button><p class="muted">来源与仓库可停用，原迁移记录保留。</p></aside>
          <section class="surface repository-panel"><div class="section-heading"><div><div class="title-line"><h3>{{ activeSource?.name || '全部来源' }}</h3><el-tag v-if="activeSource" :type="activeSource.test?.status==='success'?'success':activeSource.test?.status==='failed'?'danger':'info'">{{ sourceTestLabel(activeSource) }}</el-tag></div><p v-if="activeSource" class="muted source-url">{{ activeSource.url }}</p></div><div v-if="activeSource" class="button-group"><el-button @click="editSource(activeSource)">编辑来源</el-button><el-button :disabled="activeSource.archived" @click="discoverSource(activeSource)">连接并发现项目</el-button><el-button :disabled="activeSource.archived" @click="newRepositories(activeSource)">高级登记</el-button></div></div>
            <el-alert class="source-notice" :type="activeSource?.test?.status==='failed'?'error':activeSource?.test?.status==='success'?'success':'info'" :title="activeSource?.test?.message || '连接成功不代表读取范围完整，请确认仓库清单与完整历史读取权限。'" show-icon :closable="false" />
            <div class="toolbar"><el-input v-model="search" placeholder="搜索仓库或路径" clearable class="search" aria-label="搜索仓库或路径"/><el-select v-model="repoFilter" aria-label="迁移结果筛选" placeholder="全部迁移结果" class="filter-select"><el-option label="全部迁移结果" value=""/><el-option label="已迁移" value="success"/><el-option label="未迁移" value="unmigrated"/></el-select></div>
            <el-table :data="visibleRepositories" row-key="id" class="repository-table"><el-table-column width="44"><template #header><el-checkbox aria-label="选择当前列表全部可用仓库" :model-value="allSelected" :indeterminate="someSelected && !allSelected" @change="selectVisible"/></template><template #default="{row}"><el-checkbox :aria-label="'选择仓库 '+row.name" :disabled="row.archived || sourceById(row.source_id)?.archived" :model-value="selection.ids.includes(row.id)" @change="checked=>choose(row.id,checked)"/></template></el-table-column><el-table-column label="仓库与布局" min-width="160"><template #default="{row}"><strong>{{ row.name }}</strong><small class="muted block">{{ sourceById(row.source_id)?.name }} · {{ layoutLabel(row.layout) }}</small><small class="muted block">{{ row.inventory.revision?'r'+row.inventory.revision:'尚未盘点' }}<span v-if="row.archived"> · 已停用</span></small></template></el-table-column><el-table-column label="迁移结果" min-width="100"><template #default="{row}"><el-tag :type="row.last_success?'success':tone(row.migration?.status)">{{ migrationLabel(row) }}</el-tag><el-button v-if="row.migration" link type="primary" @click="openRun(row.migration.run_id)">任务 #{{ row.migration.run_id }}</el-button><small v-if="row.last_success && row.last_success.run_id!==row.migration?.run_id" class="muted block">此前任务 #{{ row.last_success.run_id }} 已成功</small></template></el-table-column><el-table-column label="最近预检" min-width="225"><template #default="{row}"><el-tag :type="checkTone(row.preflight)">{{ checkLabel(row.preflight) }}</el-tag><small v-if="row.preflight" class="muted block">{{ row.preflight.plan_name }} · #{{ row.preflight.id }}</small><small v-if="row.preflight" class="muted block">{{ dateTime(row.preflight.created_at) }}{{ row.preflight.current?'':' · 已过期或未完成' }}</small><p v-if="row.preflight?.current && row.preflight.issues?.length" class="inline-issue">{{ row.preflight.issues[0].message }}</p></template></el-table-column><el-table-column label="操作" min-width="115"><template #default="{row}"><div class="row-actions"><el-button v-if="row.preflight" link type="primary" @click="openCheck(row.preflight.id)">{{ row.preflight.current?'查看预检':'查看旧预检' }}</el-button><el-button link @click="editRepository(sourceById(row.source_id),row)">编辑仓库</el-button></div></template></el-table-column></el-table>
            <div class="selection-bar"><div><strong>已选 {{ selectedRepos.length }} 个仓库 · 来自 {{ selectedSourceCount }} 个来源</strong><small class="muted block">{{ selectedSummary || '可跨来源选择，切换来源保留勾选' }}</small></div><div class="button-group"><el-button link :disabled="!selectedRepos.length" @click="selectionDialog=true">查看已选</el-button><el-button link :disabled="!selectedRepos.length" @click="selection.ids=[]">清空</el-button><el-button type="primary" :disabled="!selectedRepos.length" @click="newPlan">创建迁移计划<el-icon><Right /></el-icon></el-button></div></div>
          </section></div><p class="footnote">这里只显示仓库最近一次相关预检。全部计划的问题请到迁移工作台查看；新计划仍须重新预检。</p>
      </template>
      <section v-else class="surface"><el-table :data="state.runs" row-key="id"><el-table-column label="任务 / 计划" min-width="200"><template #default="{row}"><strong>{{ row.plan_name || '历史计划 #'+row.plan_id }}</strong><small class="muted block">执行任务 #{{ row.id }} · 计划 #{{ row.plan_id }}</small></template></el-table-column><el-table-column label="状态" width="120"><template #default="{row}"><el-tag :type="tone(row.status)">{{ row.status_label }}</el-tag></template></el-table-column><el-table-column label="仓库完成量" width="140"><template #default="{row}">{{ row.completed }} / {{ row.total }}</template></el-table-column><el-table-column label="提交时间" min-width="140"><template #default="{row}">{{ dateTime(row.created_at) }}</template></el-table-column><el-table-column prop="actor_name" label="发起人" min-width="120"/><el-table-column prop="error" label="最近错误" min-width="170" show-overflow-tooltip/><el-table-column label="操作" width="120"><template #default="{row}"><el-button @click="openRun(row.id)">查看详情</el-button></template></el-table-column><template #empty><el-empty description="尚未提交迁移执行" :image-size="70"/></template></el-table></section>
      <details class="resource-details"><summary>执行器与工作区</summary><p>执行器{{ state.health?.worker?.online?'在线':'离线' }} · 可用空间 {{ size(state.health?.disk_free) }} · 工作区占用 {{ size(state.health?.workspace_bytes) }}</p><p>本期仅迁移内容和历史，不创建业务账号、不导入 SVN 权限，目标初始保持私有。</p></details>
    </template>
    <el-dialog v-model="selectionDialog" title="已选择的仓库" width="min(700px,94vw)"><div v-for="repo in selectedRepos" :key="repo.id" class="scope-row"><span>{{ repo.source_name }} / {{ repo.name }}</span><el-button link type="danger" @click="choose(repo.id,false)">移除</el-button></div><template #footer><el-button @click="selectionDialog=false">返回选择</el-button><el-button type="primary" :disabled="!selectedRepos.length" @click="selectionDialog=false;newPlan()">创建迁移计划</el-button></template></el-dialog>
    <el-dialog v-model="sourceDialog" :title="sourceForm.id ? '编辑 SVN 来源' : '添加 SVN 来源'" width="min(660px, 94vw)">
      <SvnSteps :active="0" :labels="['连接实例','自动发现项目','勾选并纳管','创建迁移计划']" />
      <el-form label-position="top" @submit.prevent="saveSource">
        <el-form-item label="来源名称"><el-input v-model="sourceForm.name" maxlength="200" /></el-form-item>
        
        <p class="muted">填写实例入口，自动发现当前账号可访问的项目，无需先填项目 URL。</p><el-form-item label="实例地址（http、https 或 svn）"><el-input v-model="sourceForm.url" placeholder="https://svn.example.test/svn" /></el-form-item>
        <el-form-item label="完整历史读取账号"><el-input v-model="sourceForm.username" autocomplete="off" /></el-form-item>
        <el-form-item :label="sourceForm.password_set ? '密码（留空保留已保存密码）' : '密码'"><el-input v-model="sourceForm.password" type="password" show-password autocomplete="new-password" /></el-form-item>
        <details><summary>高级设置</summary><el-form-item label="唯一英文标识（创建后固定）"><el-input v-model="sourceForm.slug" :disabled="!!sourceForm.id" /></el-form-item><el-form-item label="公司 CA 证书（PEM，可选）"><el-input v-model="sourceForm.ca_pem" type="textarea" :rows="3" /></el-form-item></details>
        <el-checkbox v-if="sourceForm.id" v-model="sourceForm.archived">停用来源，保留历史记录</el-checkbox>
      </el-form><template #footer><el-button @click="sourceDialog=false">取消</el-button><el-button type="primary" :loading="saving" @click="saveSource">连接并发现项目</el-button></template>
    </el-dialog>
    <el-dialog v-model="repositoryDialog" :title="repositoryForm.id ? '编辑仓库' : '批量登记仓库'" width="min(760px, 94vw)" destroy-on-close>
      <p class="muted">每行填写“英文目标仓库名 URL”，可登记一个 SVN 仓库下的不同项目。服务器地址不代表全部仓库清单。</p>
      <el-input v-if="!repositoryForm.id" v-model="repositoryLines" type="textarea" :rows="5" placeholder="project-a https://svn.example.test/svn/project-a&#10;project-b https://svn.example.test/svn/project-b" />
      <template v-else><el-input v-model="repositoryForm.name" placeholder="目标仓库名" /><el-input v-model="repositoryForm.url" placeholder="仓库 URL" /></template>
      <el-radio-group v-model="layoutMode" class="toolbar" aria-label="布局识别方式"><el-radio value="project">完整项目（推荐）</el-radio><el-radio value="auto">按指定布局迁移（高级）</el-radio><el-radio value="manual">手动配置（高级）</el-radio></el-radio-group>
      <SvnLayoutDetection v-if="layoutMode==='auto'" ref="layoutDetector" :source-id="repositoryForm.source_id" :repositories="repositoryInputs" @update:layouts="detectedLayouts=$event" />
      <el-alert v-else-if="layoutMode==='project'" title="完整纳管所填项目，迁移预检自动识别内部布局；其他目录保留在同一仓库的完整项目归档分支。" type="info" :closable="false"/><el-form v-else label-position="top" class="layout-form"><el-form-item label="主干相对路径（整个登记目录填 .）"><el-input v-model="repositoryForm.layout.trunk" /></el-form-item><el-form-item label="分支相对路径（没有则留空）"><el-input v-model="repositoryForm.layout.branches" /></el-form-item><el-form-item label="标签相对路径（没有则留空）"><el-input v-model="repositoryForm.layout.tags" /></el-form-item></el-form>
      <el-form-item label="默认 Git 分支"><el-input v-model="repositoryForm.layout.default_branch" /></el-form-item>
      <el-checkbox v-if="repositoryForm.id" v-model="repositoryForm.archived">停用仓库，保留迁移记录</el-checkbox>
      <template #footer><el-button @click="repositoryDialog=false">取消</el-button><el-button type="primary" :loading="saving" :disabled="layoutMode==='auto' && !detectedLayouts.length" @click="saveRepositories">保存仓库清单</el-button></template>
    </el-dialog>
    <el-dialog v-model="previewDialog" title="迁移预览与停写确认" width="min(1050px, 96vw)">
      <template v-if="currentPlan"><SvnSteps :active="2" /><div class="toolbar"><strong>{{ currentPlan.name }}</strong><el-tag>{{ currentPlan.status_label }}</el-tag><el-button @click="preflight(currentPlan)">重新预检</el-button></div><el-alert v-if="currentPlan.error" :title="currentPlan.error" type="error" :closable="false" />
        <el-table v-if="currentPlan.preview.issues?.length" :data="currentPlan.preview.issues"><el-table-column prop="url" label="受影响仓库" min-width="200" /><el-table-column prop="message" label="需要处理的问题" min-width="300" /></el-table>
        <el-table :data="currentPlan.preview.repositories || []"><el-table-column prop="source_name" label="来源" width="120" /><el-table-column prop="url" label="源仓库" min-width="200" show-overflow-tooltip /><el-table-column label="目标" min-width="180"><template #default="{row}">{{ row.owner }}/{{ row.name }}</template></el-table-column><el-table-column label="修订 / 作者" width="140"><template #default="{row}">r{{ row.revision }} / {{ row.authors?.length || 0 }} 人</template></el-table-column></el-table>
        <details v-if="currentPlan.preview.authors?.length"><summary>查看 {{ currentPlan.preview.authors.length }} 条作者映射</summary><el-table :data="currentPlan.preview.authors"><el-table-column prop="source_id" label="来源 ID" width="90" /><el-table-column prop="svn_user" label="SVN 作者" /><el-table-column prop="name" label="Git 署名" /><el-table-column prop="email" label="Git 邮箱" min-width="220" /><el-table-column label="身份" width="110"><template #default="{row}">{{ row.placeholder ? '历史占位身份' : '指定映射' }}</template></el-table-column></el-table></details>
        <p v-if="currentPlan.config.excluded?.length" class="muted">本计划明确排除 {{ currentPlan.config.excluded.length }} 个仓库，将保留在报告中。</p>
        <div class="confirmations"><el-checkbox v-model="fullRead">来源管理员已确认仓库清单完整，迁移账号可读取完整范围及历史</el-checkbox><el-checkbox v-model="frozen">所选源仓库已备份并停止写入；此次预检是在停写之后生成</el-checkbox></div>
        <el-alert title="如停写后又有新提交，必须重新预检。目录级权限不在本期转换；externals 会阻断迁移。" type="info" :closable="false" />
      </template><template #footer><el-button @click="previewDialog=false">关闭</el-button><el-button type="primary" :disabled="currentPlan?.status!=='ready' || (currentPlan?.preflight && !currentPlan.preflight.current) || !fullRead || !frozen" :loading="saving" @click="startRun">按预览顺序迁移</el-button></template>
    </el-dialog>
    <SvnProjectDiscovery v-if="discoverySource" :source="discoverySource" @close="discoverySource=null" @edit="editDiscoverySource" @enrolled="onEnrolled" />
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch, onMounted } from 'vue'
import '../styles/svnMigration.css'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh, InfoFilled, MoreFilled, Warning, FolderOpened, Right } from '@element-plus/icons-vue'
import { api } from '../api'
import { useOperationPolling } from '../composables/useOperationPolling'
import { useSvnSelection } from '../composables/useSvnSelection.js'
import SvnPreflightDetail from '../components/SvnPreflightDetail.vue'
import SvnRunDetail from '../components/SvnRunDetail.vue'
import SvnLayoutDetection from '../components/SvnLayoutDetection.vue'
import SvnPlanEditor from '../components/SvnPlanEditor.vue'
import SvnSteps from '../components/SvnSteps.vue'
import SvnProjectDiscovery from '../components/SvnProjectDiscovery.vue'
import { tone, checkLabel, checkTone, migrationLabel, dateTime } from '../components/svnMigrationDisplay.js'

const center=ref(null)
onMounted(()=>center.value?.closest('.app-main')?.scrollTo({top:0,left:0}))
const base='/svn-migrations',route=useRoute(),router=useRouter(),selection=useSvnSelection()
const page=computed(()=>route.meta.svnPage || 'workbench')
const state=reactive({sources:[],repositories:[],plans:[],runs:[],targets:[],health:null})
const search=ref(''),sourceSearch=ref(''),sourceId=ref(null),repoFilter=ref(''),planFilter=ref('')
const stateLoaded=ref(false)
const routeSelectedIds=computed(()=>String(route.query.repositories||'').split(',').map(Number).filter(id=>Number.isInteger(id)&&id>0))
const planRepositories=computed(()=>state.repositories.map(row=>({...row,source_name:state.sources.find(s=>s.id===row.source_id)?.name})))
const saving=ref(false),refreshing=ref(false),detailError=ref(''),runRequestKey=ref('')
const discoverySource=ref(null),justEnrolled=ref(false)
const sourceDialog=ref(false),repositoryDialog=ref(false),previewDialog=ref(false),selectionDialog=ref(false)
const sourceForm=ref({}),repositoryForm=ref({layout:{}}),repositoryLines=ref('')
const layoutMode=ref('auto'),layoutDetector=ref(null),detectedLayouts=ref([])
const repositoryInputs=computed(()=>{
 if(repositoryForm.value.id)return [{name:repositoryForm.value.name,url:repositoryForm.value.url}]
 const rows=repositoryLines.value.split('\n').map(line=>line.trim()).filter(Boolean).map(line=>{const at=line.search(/\s/);return at>0?{name:line.slice(0,at),url:line.slice(at).trim()}:null});return rows.every(Boolean)?rows:[]
})
watch(layoutMode,()=>{detectedLayouts.value=[]})
const currentPlan=ref(null),currentRun=ref(null),currentCheck=ref(null),runEvents=ref([]),fullRead=ref(false),frozen=ref(false)
const sourceById=id=>state.sources.find(s=>s.id===id)
const targetName=id=>state.targets.find(s=>s.id===id)?.name || '目标实例 #'+id
const sourceRepositories=id=>state.repositories.filter(r=>r.source_id===id)
const selectedRepos=computed(()=>state.repositories.filter(r=>selection.ids.includes(r.id)&&!r.archived&&!sourceById(r.source_id)?.archived).map(r=>({...r,source_name:sourceById(r.source_id)?.name})))
const selectedSourceCount=computed(()=>new Set(selectedRepos.value.map(r=>r.source_id)).size)
const selectedSummary=computed(()=>[...new Set(selectedRepos.value.map(r=>r.source_id))].map(id=>sourceById(id)?.name+' '+selectedRepos.value.filter(r=>r.source_id===id).length+' 个').join(' · '))
const activeSource=computed(()=>sourceById(sourceId.value))
const filteredSources=computed(()=>state.sources.filter(s=>[s.name,s.slug,s.url].join(' ').toLowerCase().includes(sourceSearch.value.toLowerCase())))
const visibleRepositories=computed(()=>state.repositories.filter(r=>(sourceId.value===null||r.source_id===sourceId.value)&&[r.name,r.url,sourceById(r.source_id)?.name].join(' ').toLowerCase().includes(search.value.toLowerCase())&&(!repoFilter.value||(repoFilter.value==='success'?!!r.last_success:!r.last_success))))
const selectable=computed(()=>visibleRepositories.value.filter(r=>!r.archived&&!sourceById(r.source_id)?.archived))
const allSelected=computed(()=>selectable.value.length>0&&selectable.value.every(r=>selection.ids.includes(r.id)))
const someSelected=computed(()=>selectable.value.some(r=>selection.ids.includes(r.id)))
function choose(id,checked){selection.ids=checked?[...new Set([...selection.ids,id])]:selection.ids.filter(i=>i!==id)}
function selectVisible(checked){const ids=selectable.value.map(r=>r.id);selection.ids=checked?[...new Set([...selection.ids,...ids])]:selection.ids.filter(id=>!ids.includes(id))}
function layoutLabel(value){if(value.project)return '完整项目 · 自动识别';if(!value.branches&&!value.tags)return '仅主干';return value.trunk==='trunk'&&value.branches==='branches'&&value.tags==='tags'?'标准布局':'自定义布局'}
function sourceTestLabel(source){return {success:'连接正常',failed:'连接失败',queued:'检查排队中'}[source.test?.status] || (source.archived?'已停用':'尚未检测')}
function size(bytes){return bytes==null?'未知':(bytes/1024**3).toFixed(1)+' GiB'}
function latestRun(plan){return state.runs.find(r=>r.plan_id===plan.id)}
function planSourceCount(plan){return new Set(plan.config.repositories.map(r=>state.repositories.find(x=>x.id===r.id)?.source_id).filter(Boolean)).size}
function planState(plan){
 const last=latestRun(plan)
 if(last&&last.plan_version===plan.version&&last.preflight_id===plan.preflight?.id)return {key:last.status,label:last.status_label,type:tone(last.status)}
 if(['queued','preflight'].includes(plan.status))return {key:plan.status,label:plan.status==='queued'?'预检排队中':'预检中',type:'primary'}
 if(plan.preflight&&!plan.preflight.current)return {key:'stale',label:'需重新预检',type:'info'}
 return {key:plan.status,label:{ready:'待确认停写',blocked:'预检受阻',draft:'草稿'}[plan.status]||plan.status_label,type:plan.status==='ready'?'warning':tone(plan.status)}
}
function planActionLabel(plan){return {stale:'重新预检',blocked:'查看预检',ready:'检查并执行'}[planState(plan).key]||'查看计划'}
function actOnPlan(plan){const key=planState(plan).key;return key==='stale'?preflight(plan):key==='blocked'&&plan.preflight?openCheck(plan.preflight.id):openPlan(plan)}
const visiblePlans=computed(()=>state.plans.filter(p=>!planFilter.value||planState(p).key===planFilter.value))
const todos=computed(()=>state.plans.flatMap(plan=>{
 if(['queued','preflight'].includes(plan.status))return []
 const check=plan.preflight,last=latestRun(plan)
 if(last&&last.plan_version===plan.version&&last.preflight_id===check?.id)return []
 if(check&&!check.current)return [{key:'stale-'+plan.id,plan,check,type:'info',message:'输入已变化或检查未完成，需重新预检'}]
 const issues=plan.preview?.issues||[]
 if(issues.length)return issues.map((issue,index)=>({key:plan.id+'-'+index,plan,check,type:'danger',message:issue.message,repository:state.repositories.find(r=>r.id===issue.repository_id)?.name||'计划范围'}))
 if(plan.error)return [{key:'error-'+plan.id,plan,check,type:'danger',message:plan.error}]
 if(plan.status==='ready')return [{key:'confirm-'+plan.id,plan,type:'warning',message:'请检查预览并确认完整读取与停写，再提交执行'}]
 return []
}))
const blockerCount=computed(()=>todos.value.filter(t=>t.type==='danger').length)
function navigate(section){return router.push({path:section==='workbench'?base:base+'/'+section})}
function openCheck(id){return router.push({path:base+'/preflights/'+id,query:{from:page.value==='sources'||(page.value==='preflight'&&route.query.from==='sources')?'sources':'workbench'}})}
function backFromCheck(){return navigate(route.query.from==='sources'?'sources':'workbench')}
function openRun(id){return router.push(base+'/runs/'+id)}
async function load(options={}){
 const routePath=route.fullPath,section=page.value,runId=route.params.runId,preflightId=route.params.preflightId
 const after=currentRun.value?.id===Number(runId)?runEvents.value.at(-1)?.id||0:0
 const [sources,plans,runs,targets,health]=await Promise.all(['sources','plans','runs','targets','status'].map(p=>api.get(base+'/'+p,options)))
 const repos=await Promise.all(sources.data.map(s=>api.get(`${base}/sources/${s.id}/repositories`,options)))
 let detail=null,events=null,check=null
 if(section==='run'){
  detail=(await api.get(`${base}/runs/${runId}`,options)).data
  events=(await api.get(`${base}/runs/${detail.id}/events`,{...options,params:{after}})).data
 }
 if(section==='preflight')check=(await api.get(`${base}/preflights/${preflightId}`,options)).data
 return {routePath,sources:sources.data,plans:plans.data,runs:runs.data,targets:targets.data,health:health.data,repositories:repos.flatMap(r=>r.data),detail,events,check}
}
function apply(data){if(data.routePath!==route.fullPath)return;stateLoaded.value=true;const {detail,events,check,routePath,...main}=data;Object.assign(state,main);detailError.value='';if(currentPlan.value){const replacement=state.plans.find(p=>p.id===currentPlan.value.id);if(replacement&&(replacement.version!==currentPlan.value.version||replacement.preview_hash!==currentPlan.value.preview_hash||replacement.preflight?.id!==currentPlan.value.preflight?.id)){fullRead.value=false;frozen.value=false;runRequestKey.value=requestKey()}currentPlan.value=replacement||currentPlan.value}if(detail)currentRun.value=detail;if(events)runEvents.value=[...new Map([...runEvents.value,...events].map(e=>[e.id,e])).values()].slice(-500);if(check)currentCheck.value=check}
const {refresh:pollRefresh,error:pollingError}=useOperationPolling(async options=>{try{return await load(options)}catch(error){detailError.value=error.response?.data?.error||'暂时无法连接服务器';throw error}},apply,()=>state.plans.some(p=>['queued','preflight'].includes(p.status))||state.runs.some(r=>['queued','running'].includes(r.status))||state.sources.some(s=>s.test?.status==='queued'))
watch(()=>route.fullPath,()=>{currentRun.value=null;currentCheck.value=null;runEvents.value=[];detailError.value='';pollRefresh()})
async function refresh(){refreshing.value=true;try{await pollRefresh()}finally{refreshing.value=false}}
async function save(action){if(saving.value)return;saving.value=true;try{await action();await refresh()}catch(error){if(!error.isAxiosError)ElMessage.error(error.message||'操作未完成，请重试')}finally{saving.value=false}}
function newSource(){sourceForm.value={name:'',slug:'svn-'+requestKey().slice(0,16),url:'',username:'',password:'',ca_pem:'',archived:false};sourceDialog.value=true}
function editSource(row){sourceForm.value={...row,password:''};sourceDialog.value=true}
async function saveSource(){await save(async()=>{const row=sourceForm.value;const {data}=await api[row.id?'put':'post'](base+'/sources'+(row.id?'/'+row.id:''),row);sourceDialog.value=false;sourceId.value=data.id;if(!data.archived)discoverySource.value=data})}
function discoverSource(row){discoverySource.value=row}
function editDiscoverySource(){const row=discoverySource.value;discoverySource.value=null;editSource(row)}
async function onEnrolled(rows){discoverySource.value=null;await refresh();selection.ids=rows.map(r=>r.id);justEnrolled.value=true;newPlan()}
async function testSource(row){await api.post(`${base}/sources/${row.id}/test`);ElMessage.success('连接检查已排队');pollRefresh()}
const defaultLayout=()=>({trunk:'trunk',branches:'branches',tags:'tags',default_branch:'main'})
function newRepositories(source){repositoryForm.value={source_id:source.id,layout:defaultLayout()};repositoryLines.value='';layoutMode.value='project';detectedLayouts.value=[];repositoryDialog.value=true}
function editRepository(source,row){repositoryForm.value={...row,source_id:source.id,layout:{...row.layout}};layoutMode.value=row.layout.project?'project':'manual';detectedLayouts.value=[];repositoryDialog.value=true}
async function saveRepositories(){await save(async()=>{const row=repositoryForm.value;let rows;if(layoutMode.value==='auto'){if(!detectedLayouts.value.length)throw Error('请先完成目录检测并确认迁移范围');rows=detectedLayouts.value.map(result=>({...result,layout:{...result.layout,default_branch:row.layout.default_branch}}))}else if(layoutMode.value==='project'){rows=(row.id?[{name:row.name,url:row.url}]:repositoryInputs.value).map(r=>({...r,layout:{trunk:'.',branches:'',tags:'',default_branch:row.layout.default_branch,project:true}}));if(!rows.length)throw Error('请填写有效的项目名称和 URL')}else{rows=repositoryLines.value.split('\n').map(l=>l.trim()).filter(Boolean).map(l=>{const at=l.search(/\s/);if(at<0)throw Error('每行需要英文仓库名及 URL');return {name:l.slice(0,at),url:l.slice(at).trim(),layout:row.layout}})}if(row.id){await api.put(`${base}/sources/${row.source_id}/repositories/${row.id}`,{...row,...(['auto','project'].includes(layoutMode.value)?rows[0]:{})})}else{await api.post(`${base}/sources/${row.source_id}/repositories`,{repositories:rows})}repositoryDialog.value=false}).catch(e=>{if(!e.response)ElMessage.error(e.message)})}
function newPlan(){return router.push({path:base+'/plans/new',query:{repositories:selectedRepos.value.map(row=>row.id).join(',')}})}
function editPlan(row){return router.push(base+'/plans/'+row.id+'/edit')}
async function onPlanSaved(row){if(row.preflight?.id)await openCheck(row.preflight.id);else await navigate('workbench')}
async function planById(id){return state.plans.find(p=>p.id===id)||(await api.get(`${base}/plans/${id}`)).data}
async function editPlanById(id){editPlan(await planById(id))}
async function showPlan(id){openPlan(await planById(id))}
async function preflightById(id){await preflight(await planById(id))}
function openPlan(row){currentPlan.value=row;fullRead.value=false;frozen.value=false;runRequestKey.value=requestKey();previewDialog.value=true}
async function preflight(row){await save(async()=>{const {data}=await api.post(`${base}/plans/${row.id}/preflight`);fullRead.value=false;frozen.value=false;if(page.value==='preflight'&&data.preflight)await router.push({path:`${base}/preflights/${data.preflight.id}`,query:{from:route.query.from==='sources'?'sources':'workbench'}})})}
function requestKey(){return Array.from(crypto.getRandomValues(new Uint8Array(16)),byte=>byte.toString(16).padStart(2,'0')).join('')}
async function startRun(){if(saving.value)return;await save(async()=>{const result=await api.post(base+'/runs',{plan_id:currentPlan.value.id,preview_hash:currentPlan.value.preview_hash,request_key:runRequestKey.value,full_read_confirmed:fullRead.value,freeze_confirmed:frozen.value});previewDialog.value=false;await openRun(result.data.id)})}
async function controlRun(action){if(saving.value)return;if(action==='cancel')await ElMessageBox.confirm('取消后保留已创建的仓库和现场，后续可核对恢复。','取消迁移');await save(()=>api.post(`${base}/runs/${currentRun.value.id}/${action}`))}
async function downloadReport(){const result=await api.get(`${base}/runs/${currentRun.value.id}/report`,{responseType:'blob'});const url=URL.createObjectURL(result.data);const link=document.createElement('a');link.href=url;link.download=`svn-migration-${currentRun.value.id}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
async function cleanupRun(){await ElMessageBox.confirm('仅删除此成功任务的本地转换和验证文件，远端仓库、迁移记录与审计仍保留。','清理工作区');await api.post(`${base}/runs/${currentRun.value.id}/cleanup`);ElMessage.success('工作区已清理')}
</script>
