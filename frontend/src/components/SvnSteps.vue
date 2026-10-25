<template>
  <ol class="svn-steps" aria-label="迁移步骤">
    <li v-for="(label,index) in labels" :key="label" :class="{completed:index<active,current:index===active}" :aria-current="index===active?'step':undefined">
      <div class="step-heading"><span class="step-circle"><el-icon v-if="index<active"><Check /></el-icon><span v-else>{{ index+1 }}</span></span><span class="step-label">{{ label }}</span></div>
      <span v-if="index<labels.length-1" class="step-connector" aria-hidden="true" />
    </li>
  </ol>
</template>
<script setup>
import { Check } from '@element-plus/icons-vue'
defineProps({ active:{type:Number,default:0}, labels:{type:Array,default:()=>['选择项目','选择目标','预检','执行迁移']} })
</script>
<style scoped>
.svn-steps{display:flex;align-items:center;list-style:none;padding:18px 0 24px;margin:0;width:100%;color:#526987;gap:16px;box-sizing:border-box}.svn-steps li{display:flex;align-items:center;flex:1;min-width:0;gap:16px}.svn-steps li:last-child{flex:0 0 auto}.step-heading{display:flex;align-items:center;gap:12px;flex:0 0 auto}.step-circle{display:inline-flex;align-items:center;justify-content:center;flex:0 0 40px;width:40px;height:40px;border:1px solid #b9cce9;border-radius:50%;font-size:18px;background:#f5f9ff;color:#21304f;box-sizing:border-box}.step-label{font-size:16px;line-height:1.5;white-space:nowrap}.step-connector{height:2px;background:#c8d8ef;flex:1;min-width:18px}.completed .step-circle{background:#298bff;border-color:#298bff;color:#fff}.completed .step-connector{background:#298bff}.current{color:#0670f9;font-weight:600}.current .step-circle{background:#0670f9;border-color:#0670f9;color:#fff}.step-circle .el-icon{font-size:23px}@media(max-width:800px){.svn-steps{gap:8px}.svn-steps li{gap:8px}.step-heading{gap:8px}.step-label{font-size:13px}.step-circle{flex-basis:30px;width:30px;height:30px;font-size:15px}}@media(max-width:560px){.svn-steps{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;padding:12px 0 18px}.svn-steps li:last-child{flex:1}.step-connector{display:none}.step-label{white-space:normal}}
</style>
