<template>
  <el-dialog :model-value="true" title="SVN 实例发现与项目纳管" width="min(1000px, 96vw)" @update:model-value="emit('close')">
    <div class="svn-discovery">
      <SvnSteps :active="probe?.status==='ready'?2:1" :labels="['连接实例','自动发现项目','勾选并纳管','创建迁移计划']" />
      <div class="discovery-source"><strong>{{ source.name }}</strong><span>{{ source.url }}</span><el-tag v-if="probe?.result?.connected" type="success">连接成功</el-tag></div>
      <p v-if="probe?.result?.entry_urls?.length" class="muted">已识别项目列表入口：{{ probe.result.entry_urls.join('、') }}</p>
      <template v-if="running">
        <h3>正在读取可访问项目</h3><p class="muted">使用已保存的读取账号，只读扫描，不修改 SVN。</p>
        <el-progress :percentage="progress" :indeterminate="!probe?.result?.total" />
        <p>已验证 {{ probe?.result?.projects?.length || 0 }} 个项目<span v-if="probe?.result?.total"> · 已处理 {{ probe.result.processed }} / {{ probe.result.total }} 个入口</span></p>
        <ul class="discovery-found"><li v-for="row in probe?.result?.projects || []" :key="row.key"><el-icon><FolderOpened /></el-icon>{{ row.label }}<el-tag type="success">已发现</el-tag></li></ul>
      </template>
      <el-alert v-if="error || pollingError || probe?.error" :title="error || probe?.error || pollingError" type="error" :closable="false" show-icon />
      <template v-if="probe?.status==='ready'">
        <div class="discovery-toolbar"><h3>发现 {{ projects.length }} 个可访问项目</h3><el-input v-model="search" placeholder="搜索项目名称或路径" clearable aria-label="搜索发现项目"/><el-checkbox :model-value="all" :indeterminate="some && !all" @change="toggleAll">全选</el-checkbox></div>
        <el-table :data="visible" row-key="key" class="discovery-table">
          <el-table-column width="48"><template #default="{row}"><el-checkbox :model-value="selected.includes(row.key)" :aria-label="'纳管项目 '+row.label" :disabled="!!row.managed_id" @change="checked=>toggle(row.key,checked)"/></template></el-table-column>
          <el-table-column label="项目名称" min-width="190"><template #default="{row}"><strong>{{ row.label }}</strong><el-button link type="primary" @click="expanded=expanded===row.key?'':row.key">查看目录</el-button><div v-if="expanded===row.key" class="discovery-directories"><span v-for="entry in row.entries" :key="entry.name">{{ entry.name }}{{ entry.kind==='dir'?'/':'' }}</span><span v-if="!row.entries.length">空目录</span></div></template></el-table-column>
          <el-table-column label="实际路径" min-width="180"><template #default="{row}"><span class="discovery-url">{{ row.url }}</span></template></el-table-column>
          <el-table-column label="访问状态" width="110"><template #default="{row}"><el-tag :type="row.managed_id?'info':'success'">{{ row.managed_id?'已纳管':'可访问' }}</el-tag></template></el-table-column>
        </el-table>
        <p class="muted">勾选即纳管整个项目；内部目录保持归属，不另行拆分。目录转换与完整内容覆盖在迁移预检中核对。</p>
      </template>
      <template v-if="probe?.status==='unavailable' || probe?.status==='failed' || probe?.status==='stale'">
        <el-alert v-if="probe.status==='unavailable'" :title="probe.result.message" type="warning" show-icon :closable="false"/>
        <el-alert v-if="probe.status==='stale'" title="连接配置已变化，请重新发现项目" type="warning" :closable="false"/>
        <p class="muted">仅在自动发现不可用时，可补充一个已知项目地址继续识别，无需填写内部目录。</p>
        <el-button v-if="probe.status==='failed'" @click="emit('edit')">修改连接信息</el-button>
        <el-button @click="fallback=!fallback">补充项目地址</el-button>
        <div v-if="fallback" class="discovery-fallback"><el-input v-model="fallbackUrl" aria-label="补充项目地址" placeholder="https://svn.example.test/svn/project"/><el-button type="primary" :disabled="!fallbackUrl.trim()" @click="start(fallbackUrl.trim())">识别此项目</el-button></div>
      </template>
    </div>
    <template #footer><div class="discovery-footer"><strong v-if="probe?.status==='ready'">已选 {{ selected.length }} 个项目</strong><span v-else>发现不会立即执行迁移</span><div class="button-group"><el-button @click="emit('close')">关闭</el-button><el-button :disabled="running||busy" @click="start()">重新发现</el-button><el-button v-if="probe?.status==='ready'" type="primary" :disabled="!selected.length" :loading="busy" @click="enroll">纳管所选的 {{ selected.length }} 个项目</el-button></div></div></template>
  </el-dialog>
</template>
<script setup>
import { computed, ref, onMounted } from 'vue'
import { FolderOpened } from '@element-plus/icons-vue'
import { api } from '../api'
import { useOperationPolling } from '../composables/useOperationPolling'
import SvnSteps from './SvnSteps.vue'
const props=defineProps({source:{type:Object,required:true}}),emit=defineEmits(['close','enrolled','edit'])
const probe=ref(null),error=ref(''),busy=ref(false),starting=ref(false),search=ref(''),selected=ref([]),expanded=ref(''),fallback=ref(false),fallbackUrl=ref('')
let generation=0
const running=computed(()=>starting.value||['queued','running'].includes(probe.value?.status))
const projects=computed(()=>probe.value?.result?.projects||[])
const visible=computed(()=>projects.value.filter(p=>(p.label+' '+p.url).toLowerCase().includes(search.value.toLowerCase())))
const selectable=computed(()=>visible.value.filter(p=>!p.managed_id))
const all=computed(()=>selectable.value.length>0&&selectable.value.every(p=>selected.value.includes(p.key)))
const some=computed(()=>selectable.value.some(p=>selected.value.includes(p.key)))
const progress=computed(()=>probe.value?.result?.total?Math.min(99,Math.round(probe.value.result.processed/probe.value.result.total*100)):5)
function toggle(key,checked){selected.value=checked?[...new Set([...selected.value,key])]:selected.value.filter(k=>k!==key)}
function toggleAll(checked){const keys=selectable.value.map(p=>p.key);selected.value=checked?[...new Set([...selected.value,...keys])]:selected.value.filter(k=>!keys.includes(k))}
const {refresh,error:pollingError}=useOperationPolling(async options=>{
 const version=generation,id=probe.value?.id
 return {version,row:id?(await api.get(`/svn-migrations/discoveries/${id}`,options)).data:null}
},({version,row})=>{if(version===generation&&row)probe.value=row},()=>running.value)
async function start(url){
 if(running.value)return
 const version=++generation;starting.value=true;error.value='';selected.value=[]
 try{const {data}=await api.post(`/svn-migrations/sources/${props.source.id}/discoveries`,url?{url}:{});if(version===generation){probe.value=data;await refresh()}}catch(e){error.value=e.response?.data?.error||'无法开始发现，请重试'}finally{starting.value=false}
}
async function enroll(){
 if(busy.value||!selected.value.length)return
 busy.value=true;error.value=''
 try{const {data}=await api.post(`/svn-migrations/sources/${props.source.id}/enroll`,{discovery_id:probe.value.id,keys:selected.value});emit('enrolled',data)}catch(e){error.value=e.response?.data?.error||'纳管未完成，请重试'}finally{busy.value=false}
}
onMounted(()=>start())
</script>
<style scoped>
.svn-discovery{display:grid;gap:18px;min-width:0}.discovery-steps{list-style:none;padding:0;display:flex;gap:14px;flex-wrap:wrap;color:#64748b;font-size:13px}.discovery-steps li{padding:6px 0}.discovery-steps .done{color:#15803d}.discovery-steps .active{color:#0670f9;font-weight:600}.discovery-source,.discovery-toolbar,.discovery-footer,.discovery-fallback,.discovery-found li{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.discovery-source{padding:14px;background:#f0f7ff;border:1px solid #dce7f2;border-radius:8px}.discovery-source span,.discovery-url{overflow-wrap:anywhere;min-width:0}.discovery-toolbar h3{margin:0 auto 0 0}.discovery-toolbar .el-input{width:260px}.discovery-footer{justify-content:space-between}.discovery-directories{display:flex;gap:10px;flex-wrap:wrap;background:#f0f7ff;padding:10px;font-size:12px}.discovery-found{padding:0;list-style:none}.discovery-found li{padding:10px;border-bottom:1px solid #dce7f2}.discovery-found .el-tag{margin-left:auto}.discovery-fallback .el-input{flex:1;min-width:200px}.discovery-table{width:100%}@media(max-width:600px){.discovery-toolbar .el-input{width:100%}.discovery-footer .button-group{width:100%;display:flex;flex-wrap:wrap;gap:8px}.discovery-footer .el-button{margin-left:0}}
</style>
