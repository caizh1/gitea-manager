<template>
  <div class="svn-layout-detection">
    <el-alert title="使用已保存的 SVN 账号读取真实目录，自动识别布局；检测只读，不修改 SVN。" type="info" show-icon :closable="false" />
    <div class="toolbar"><span class="muted">{{ summary }}</span><el-button :loading="starting" :disabled="!repositories.length || running" @click="start">{{ probes.length ? '重新检测目录' : '检测目录结构' }}</el-button></div>
    <el-alert v-if="error || pollingError" :title="error || pollingError" type="error" show-icon :closable="false" />
    <section v-for="probe in probes" :key="probe.id" class="layout-detection-result">
      <div class="section-heading"><strong>{{ repositories.find(row=>row.url===probe.input_url)?.name || '仓库' }}</strong><el-tag :type="probe.status==='ready'?'success':['failed','stale'].includes(probe.status)?'danger':'warning'">{{ labels[probe.status] || probe.status }}</el-tag></div>
      <p class="muted source-url">{{ probe.url }}<span v-if="probe.result?.revision != null"> · 修订 r{{ probe.result.revision }}</span></p>
      <el-alert v-if="probe.error || probe.status==='stale'" :title="probe.status==='stale'?'来源配置已变化，请重新检测':probe.error" type="error" :closable="false" show-icon />
      <template v-if="['ready','ambiguous'].includes(probe.status)">
        <el-alert v-for="warning in probe.result.warnings" :key="warning" :title="warning" type="warning" :closable="false" show-icon />
        <template v-if="!probe.result.limited">
          <el-form-item label="检测到的项目布局">
            <el-select v-model="choices[probe.id].candidate" aria-label="检测到的项目布局" @change="selectCandidate(probe)">
              <el-option v-for="(candidate,index) in probe.result.candidates" :key="index" :value="index" :label="describe(candidate.layout)" />
              <el-option :value="-1" label="从真实目录中选择其他布局" />
            </el-select>
          </el-form-item>
          <div v-if="choices[probe.id].candidate===-1" class="layout-form">
            <el-form-item v-for="key in ['trunk','branches','tags']" :key="key" :label="roleNames[key]">
              <el-select v-model="choices[probe.id].layout[key]" :aria-label="roleNames[key]" filterable>
                <el-option v-if="key==='trunk'" value="." label="整个登记目录作为主干" />
                <el-option v-else value="" label="没有此目录" />
                <el-option v-for="directory in availableDirectories(probe,key)" :key="directory.path" :value="directory.path" :label="directory.path" />
              </el-select>
            </el-form-item>
          </div>
          <p v-else-if="choices[probe.id].layout" class="layout-summary">{{ describe(choices[probe.id].layout) }}</p>
          <el-alert v-if="invalidLayout(choices[probe.id].layout)" title="主干、分支和标签目录不能相同或相互包含；整个登记目录作为主干时不再单独配置分支和标签。" type="error" :closable="false" show-icon />
          <el-checkbox v-if="probe.status==='ambiguous'" v-model="choices[probe.id].confirmed">已确认所选项目和迁移范围</el-checkbox>
        </template>
        <details><summary>查看实际目录（最多扫描 {{ probe.result.requests }} 次目录请求）</summary>
          <p class="muted">以下名称来自 SVN 实际返回；未深入扫描的目录不代表其下没有内容。</p>
          <ul class="detected-directories"><li v-for="entry in probe.result.entries" :key="entry.name">{{ entry.name }}{{ entry.kind==='dir'?'/':'' }}</li></ul>
          <p v-if="!probe.result.entries.length" class="muted">当前地址下没有可见条目。</p>
          <ul class="detected-directories"><li v-for="directory in probe.result.directories.filter(row=>row.path.includes('/'))" :key="directory.path">{{ directory.path }}/</li></ul>
        </details>
      </template>
    </section>
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch, onUnmounted } from 'vue'
import { api } from '../api'
import { useOperationPolling } from '../composables/useOperationPolling'
const props=defineProps({sourceId:Number,repositories:{type:Array,default:()=>[]}})
const emit=defineEmits(['update:layouts'])
const probes=ref([]),choices=reactive({}),starting=ref(false),error=ref('')
let generation=0
onUnmounted(()=>{generation++})
const labels={queued:'等待执行器',running:'正在读取目录',ready:'已识别',ambiguous:'需要确认范围',failed:'检测失败',stale:'检测已失效'}
const roleNames={trunk:'SVN 主干目录',branches:'SVN 分支目录',tags:'SVN 标签目录'}
const running=computed(()=>probes.value.some(row=>['queued','running'].includes(row.status)))
const summary=computed(()=>!probes.value.length?'无需手动填写路径，先检测实际目录。':running.value?'正在由独立执行器读取 SVN，请等待。':'已读取目录，请检查识别结果后保存。')
function describe(value){return `主干：${value.trunk==='.'?'整个登记目录':value.trunk}；分支：${value.branches||'无'}；标签：${value.tags||'无'}`}
function availableDirectories(probe,key){return probe.result.directories.filter(row=>key==='trunk'||row.listed&&!row.files)}
function invalidLayout(value){const paths=['trunk','branches','tags'].map(key=>value[key]).filter(Boolean);return (value.trunk==='.'&&paths.length>1)||new Set(paths).size!==paths.length||paths.some(a=>paths.some(b=>a!==b&&b.startsWith(a+'/')))}
function selectCandidate(probe){const choice=choices[probe.id];choice.confirmed=false;choice.layout=choice.candidate===-1?{trunk:'',branches:'',tags:'',default_branch:'main'}:{...probe.result.candidates[choice.candidate].layout}}
function publish(){
 const rows=props.repositories.map(repo=>{const probe=probes.value.find(row=>row.input_url===repo.url),choice=probe&&choices[probe.id];return probe&&choice&&['ready','ambiguous'].includes(probe.status)&&!probe.result.limited&&(probe.status==='ready'||choice.confirmed)&&choice.layout?.trunk&&!invalidLayout(choice.layout)?{...repo,url:probe.url,layout:{...choice.layout},detection_id:probe.id}:null})
 emit('update:layouts',rows.length&&rows.every(Boolean)?rows:[])
}
watch(()=>[props.sourceId,JSON.stringify(props.repositories)],()=>{generation++;probes.value=[];Object.keys(choices).forEach(key=>delete choices[key]);error.value='';emit('update:layouts',[])})
watch([probes,choices],publish,{deep:true})
const {refresh,error:pollingError}=useOperationPolling(async options=>{
 const version=generation,rows=[...probes.value]
 for(let index=0;index<rows.length;index+=8){const batch=await Promise.all(rows.slice(index,index+8).map(row=>['queued','running'].includes(row.status)?api.get(`/svn-migrations/layout-detections/${row.id}`,options).then(res=>({...res.data,input_url:row.input_url})):row));rows.splice(index,batch.length,...batch)}
 return {version,rows}
},({version,rows})=>{if(version!==generation)return;probes.value=rows;for(const probe of rows){if(!choices[probe.id]&&['ready','ambiguous'].includes(probe.status)){const candidate=probe.result.candidates.length===1?0:-1;choices[probe.id]={candidate,confirmed:false,layout:candidate===0?{...probe.result.candidates[0].layout}:{trunk:'',branches:'',tags:'',default_branch:'main'}}}}},()=>running.value)
async function start(){
 if(starting.value||running.value||!props.repositories.length)return
 const version=++generation;starting.value=true;error.value='';probes.value=[];emit('update:layouts',[])
 try{const inputs=[...props.repositories];const response=await api.post(`/svn-migrations/sources/${props.sourceId}/layout-detections`,{urls:inputs.map(row=>row.url)});if(version!==generation)return;probes.value=response.data.map((row,index)=>({...row,input_url:inputs[index].url}));await refresh()}catch(e){if(version===generation)error.value=e.response?.data?.error||e.message||'目录检测请求失败'}finally{starting.value=false}
}
defineExpose({start,running})
</script>
