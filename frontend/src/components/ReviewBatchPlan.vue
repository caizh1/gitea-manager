<template>
  <section class="batch-plan" aria-label="分批审查计划">
    <h3>分批审查计划</h3>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <template v-if="task.batch">
      <p>主审 {{ plan.primary }} 批 · 关联复核 {{ plan.association }} 批 · 历史问题 {{ plan.history }} 项</p>
      <p>基础调用 {{ plan.base_calls }} 次，含候选反证最多 {{ plan.max_calls }} 次；已授权 {{ task.batch.authorized_calls }} 次。</p>
      <p>{{ plan.profile.basis }} · {{ plan.profile.model || '未知型号保守计数' }} · 资源版本 <code>{{ plan.profile.revision }}</code></p>
      <p>主审及关联初始输入合计 {{ plan.input_tokens }} Token／估算单位；每次输出额度 {{ plan.config.model_max_output_tokens || 8192 }} Token；文本变更 {{ plan.changed_lines }} 行。预计耗时：样本不足，累计有效执行预算 {{ Math.round(task.batch.budget_seconds) }} 秒。</p>
      <div v-if="task.timing" class="batch-times">
        <span>执行 {{ seconds(task.timing.execution_seconds) }}</span><span>排队 {{ seconds(task.timing.queue_seconds) }}</span>
        <span>等待确认／暂停 {{ seconds(task.timing.paused_seconds) }}</span><span>总历时 {{ seconds(task.timing.total_seconds) }}</span>
      </div>
      <p v-if="task.batch.uncertain_time">存在中断期间无法精确确认的执行时间，不作为精确性能统计。</p>
      <div class="batch-actions">
        <el-button v-if="task.status === 'awaiting_cost'" type="primary" :loading="busy" @click="act('confirm-plan')">确认此计划（最多 {{ plan.max_calls }} 次）</el-button>
        <template v-if="['budget_paused', 'incomplete'].includes(task.status)">
          <el-input-number v-model="extra" :min="0" :max="604800" aria-label="追加执行预算秒数" />
          <el-button :loading="busy" @click="act('continue')">追加秒数并继续／核对</el-button>
        </template>
      </div>
      <el-alert v-for="gap in plan.gaps" :key="gap.path" :title="gap.path + '：' + gap.reason" type="warning" :closable="false" />
      <details v-if="plan.unsupported.length"><summary>未支持内容（{{ plan.unsupported.length }}）</summary><p v-for="item in plan.unsupported" :key="item.path">{{ item.path }}：{{ item.reason }}</p></details>
      <el-table :data="task.batch.units" max-height="400">
        <el-table-column label="单元" prop="id" min-width="170" /><el-table-column label="类型" min-width="100"><template #default="{ row }">{{ kinds[row.kind] || row.kind }}</template></el-table-column>
        <el-table-column label="初始输入" prop="input_tokens" min-width="110" /><el-table-column label="状态" min-width="110"><template #default="{ row }">{{ states[row.state] || row.state }}</template></el-table-column>
        <el-table-column label="详情" min-width="200"><template #default="{ row }"><span v-if="row.error">{{ row.error }}</span><details v-if="row.result"><summary>查看单元结果</summary><pre>{{ JSON.stringify(row.result, null, 2) }}</pre></details><el-button v-if="row.kind !== 'history' && row.state === 'failed' && task.status === 'incomplete'" link :loading="busy" @click="act('continue', row.id)">明确重试该单元</el-button></template></el-table-column>
      </el-table>
      <p>必要单元全部完成前，PR 只展示状态和覆盖范围。文本覆盖率不等于缺陷召回率。</p>
    </template>
    <p v-else>{{ task.status === 'incomplete' ? '预检未完成，请查看任务诊断；修正输入或设置后重新发起审查。' : '正在生成固定版本计划，尚未调用审查模型。' }}</p>
  </section>
</template>
<script setup>
import { computed, ref } from 'vue'
import { api } from '../api'
import { errorMessage } from './reviewDisplay'
const emit = defineEmits(['refresh'])
const props = defineProps({ task: { type: Object, required: true } })
const plan = computed(() => props.task.batch?.value)
const busy = ref(false), error = ref(''), extra = ref(3600)
const kinds = { primary: '主审', association: '关联复核', refutation: '候选反证', history: '历史复核' }
const states = { pending: '待执行', running: '执行中', done: '已完成', failed: '已确认失败', unknown: '结果待核对' }
const seconds = value => value == null ? '未记录' : Math.round(value) + ' 秒'
async function act(action, unit) {
  if (busy.value) return
  busy.value = true; error.value = ''
  try {
    const csrf = (await api.get('/ai-review/csrf')).data.token
    await api.post(`/ai-review/tasks/${props.task.id}/${action}`, { plan_hash: props.task.batch.hash, extra_seconds: extra.value, ...(unit ? { retry_unit: unit } : {}) }, { headers: { 'X-Review-CSRF': csrf } })
  emit('refresh')
  } catch (e) { error.value = errorMessage(e) } finally { busy.value = false }
}
</script>
<style scoped>
.batch-plan { padding: 18px; border: 1px solid #dce4ef; border-radius: 14px; margin-block: 16px; min-width: 0; }
.batch-actions, .batch-times { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-block: 12px; }
code, pre { white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
