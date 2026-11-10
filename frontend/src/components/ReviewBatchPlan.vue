<template>
  <section class="batch-plan" :aria-label="single ? '审查计划' : '分批审查计划'">
    <div class="plan-heading"><div><h3>{{ single ? '审查计划' : '分批审查计划' }}</h3><p>查看执行单元、调用预算与源码检查依据。</p></div><el-tag v-if="plan" type="info">{{ single ? '单次主审' : '分批审查' }}</el-tag></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <template v-if="plan">
      <div class="plan-metrics">
        <div><span>调用上限</span><strong>{{ plan.max_calls ?? '—' }} <small>次</small></strong><span>基础 {{ plan.base_calls ?? '—' }} 次 · 已授权 {{ task.batch.authorized_calls ?? 0 }} 次</span></div>
        <div><span>文本变更</span><strong>{{ plan.changed_lines ?? '—' }} <small>行</small></strong><span>{{ single ? `新增 ${plan.added_lines ?? '—'} · 删除 ${plan.deleted_lines ?? '—'}` : `主审 ${plan.primary ?? 0} 批 · 关联 ${plan.association ?? 0} 批 · 历史 ${plan.history ?? 0} 项` }}</span></div>
        <div><span>有效执行预算</span><strong>{{ Math.round(task.batch.budget_seconds || 0) }} <small>秒</small></strong><span>实际执行 {{ seconds(task.timing?.execution_seconds) }}</span></div>
      </div>
      <div v-if="needsAction" class="plan-action-panel">
        <div><strong>{{ task.status === 'awaiting_cost' ? '等待确认调用上限' : '审查已暂停，等待处理' }}</strong><p>{{ task.status === 'awaiting_cost' ? '确认固定计划后开始执行，请先核对下方预算与覆盖缺口。' : '追加执行预算后继续审查；结果待核对的单元会先核对。' }}</p></div>
        <div class="batch-actions">
          <el-button v-if="task.status === 'awaiting_cost'" type="primary" :loading="busy" @click="act('confirm-plan')">确认此计划（最多 {{ plan.max_calls }} 次）</el-button>
          <template v-else><el-input-number v-model="extra" :min="0" :max="604800" aria-label="追加执行预算秒数" /><el-button type="primary" :loading="busy" @click="act('continue')">追加秒数并继续／核对</el-button></template>
        </div>
      </div>
      <el-alert v-for="(gap, index) in plan.gaps || []" :key="index" :title="gap.path + '：' + gap.reason" type="warning" :closable="false" />
      <details v-if="plan.unsupported?.length" class="plan-section warning-section"><summary>未支持内容（{{ plan.unsupported.length }}）</summary><p v-for="(item, index) in plan.unsupported" :key="index">{{ item.path }}：{{ item.reason }}</p></details>
      <section class="unit-section">
        <div class="unit-heading"><h4>执行单元</h4><span>{{ completedUnits }} / {{ task.batch.units?.length || 0 }} 已完成</span></div>
        <el-table :data="task.batch.units || []" max-height="400">
          <el-table-column label="单元" prop="id" min-width="150" /><el-table-column label="类型" min-width="100"><template #default="{ row }">{{ kinds[row.kind] || row.kind }}</template></el-table-column>
          <el-table-column label="初始输入" prop="input_tokens" min-width="100" /><el-table-column label="状态" min-width="110"><template #default="{ row }"><el-tag size="small" :type="row.state === 'done' ? 'success' : ['failed', 'unknown'].includes(row.state) ? 'warning' : 'info'">{{ states[row.state] || row.state }}</el-tag></template></el-table-column>
          <el-table-column label="详情" min-width="160"><template #default="{ row }"><span v-if="row.error">{{ row.error }}</span><details v-if="row.result"><summary>查看单元结果</summary><pre>{{ JSON.stringify(row.result, null, 2) }}</pre></details><el-button v-if="row.kind !== 'history' && row.state === 'failed' && task.status === 'incomplete'" link :loading="busy" @click="act('continue', row.id)">明确重试该单元</el-button></template></el-table-column>
        </el-table>
        <p class="plan-note">必要单元全部完成前，PR 只展示状态和覆盖范围。文本覆盖率不等于缺陷召回率。</p>
      </section>
      <details v-if="plan.relations?.length" class="plan-section">
        <summary>源码关系与检查依据（{{ plan.relations.length }}）</summary>
        <div class="relations-list"><article v-for="(relation, index) in plan.relations" :key="index" class="relation-item">
          <div class="relation-heading"><strong>关联 {{ index + 1 }}</strong><el-tag size="small" :type="relation.covered_by ? 'success' : 'info'">{{ relation.covered_by ? '同批联合检查' : '跨批关联复核' }}</el-tag></div>
          <div v-for="(code, codeIndex) in relation.code || []" :key="codeIndex" class="relation-source"><code>{{ code.path }}</code><span>{{ code.name }} · {{ code.side === 'old' ? '基线' : '修改后' }} {{ code.start }}–{{ code.end }}</span></div>
          <p>{{ relation.reason }}</p>
        </article></div>
      </details>
      <details class="plan-section"><summary>执行策略与预算明细</summary>
        <dl class="plan-facts"><dt>调用策略</dt><dd>基础调用 {{ plan.base_calls }} 次，含候选反证{{ ['diff-batches-v2', 'diff-batches-v3'].includes(plan.version) ? '与引用纠正' : '' }}最多 {{ plan.max_calls }} 次。</dd>
          <template v-if="single"><dt>单次范围</dt><dd>{{ plan.max_changed_lines ? `新增与删除合计最多 ${plan.max_changed_lines} 行` : `最多净增加 ${plan.max_net_added_lines} 行` }}。新增与删除全部纳入审查，拆分未启用；P0/P1 汇总后独立反证，超出输入额度时停止。</dd></template>
          <template v-if="!single && plan.version === 'diff-batches-v3'"><dt>关联与反证</dt><dd>按源码关系关联复核；主审完成后汇总候选并分组反证。{{ plan.refutation_planned ? `已生成 ${plan.refutation_groups} 个反证组。` : '反证分组待候选汇总。' }}</dd></template>
          <template v-if="plan.reference_protocol === 'source-locations-v3'"><dt>引用核对</dt><dd>使用源码位置与自然语言正文；评论位置由系统核对，P0/P1 逐项反证。</dd></template>
          <template v-if="plan.output_adjustments?.length"><dt>输出调整</dt><dd>已按原有关系或候选拆分 {{ plan.output_adjustments.length }} 个输出超限单元，历史记录保留。</dd></template>
          <dt>计数依据</dt><dd>{{ plan.profile?.basis || '未记录' }} · {{ plan.profile?.model || '未知型号保守计数' }} · 资源版本 {{ plan.profile?.revision || '未记录' }}</dd>
          <dt>输入与输出</dt><dd>主审及关联初始输入 {{ plan.input_tokens ?? '—' }} Token／估算单位；每次输出额度 {{ plan.config?.model_max_output_tokens || 8192 }} Token。</dd>
          <dt>预计耗时</dt><dd>样本不足，无法估计；有效执行预算不等于预计完成时间。</dd>
          <template v-if="task.timing"><dt>时间记录</dt><dd>执行 {{ seconds(task.timing.execution_seconds) }} · 排队 {{ seconds(task.timing.queue_seconds) }} · 等待确认／暂停 {{ seconds(task.timing.paused_seconds) }} · 总历时 {{ seconds(task.timing.total_seconds) }}</dd></template>
        </dl><p v-if="task.batch.uncertain_time" class="plan-note">存在中断期间无法精确确认的执行时间，不作为精确性能统计。</p>
      </details>
      <details v-if="plan.limitations?.length" class="plan-section"><summary>关系分析边界（{{ plan.limitations.length }}）</summary><p v-for="(item, index) in plan.limitations" :key="index">{{ item.path || '工程范围' }}：{{ item.reason }}</p></details>
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
const single = computed(() => plan.value?.review_mode === 'single')
const needsAction = computed(() => ['awaiting_cost', 'budget_paused', 'incomplete'].includes(props.task.status))
const completedUnits = computed(() => props.task.batch?.units?.filter(unit => unit.state === 'done').length || 0)
const busy = ref(false), error = ref(''), extra = ref(3600)
const kinds = { primary: '主审', association: '关联复核', refutation: '候选反证', history: '历史复核' }
const states = { pending: '待执行', running: '执行中', done: '已完成', failed: '已确认失败', unknown: '结果待核对', replaced: '已拆分' }
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
.batch-plan{min-width:0;padding:8px 0;color:#344761;font-size:13px}.plan-heading,.unit-heading,.relation-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.plan-heading h3{font-size:16px;margin:0}.plan-heading p,.plan-note{color:#64748b;font-size:12px;line-height:1.6}.plan-heading p{margin:6px 0 0}.plan-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:16px 0}.plan-metrics>div{display:flex;flex-direction:column;gap:7px;padding:14px 16px;border:1px solid #e0e9f5;border-radius:10px;background:#f8fbff;min-width:0}.plan-metrics span{font-size:12px;color:#64748b}.plan-metrics strong{font-size:24px;color:#243b59;font-variant-numeric:tabular-nums}.plan-metrics small{font-size:12px;font-weight:400}.plan-action-panel{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;padding:14px;border:1px solid #f0d294;border-radius:10px;background:#fffbf2;margin:12px 0}.plan-action-panel p{margin:6px 0 0;font-size:12px}.batch-actions{display:flex;align-items:center;flex-wrap:wrap;gap:10px}.batch-actions .el-button{margin:0;white-space:normal;height:auto;min-height:32px;line-height:1.5;padding:8px 12px}.unit-section{margin:18px 0}.unit-heading{margin-bottom:10px}.unit-heading h4{margin:0;font-size:14px}.unit-heading>span{font-size:12px;color:#64748b}.plan-section{margin-top:12px;border:1px solid #e1e8f1;border-radius:10px;padding:0 14px;min-width:0}.plan-section>summary{padding:13px 0;cursor:pointer;color:#344761;font-weight:500}.plan-section[open]>summary{border-bottom:1px solid #e1e8f1;margin-bottom:12px}.plan-section p{line-height:1.7;overflow-wrap:anywhere}.relations-list{display:grid;gap:10px;padding-bottom:14px}.relation-item{border:1px solid #e5ebf4;border-radius:8px;padding:12px;min-width:0}.relation-item p{margin:8px 0 0;color:#64748b}.relation-heading{margin-bottom:10px}.relation-source{display:flex;flex-direction:column;gap:4px;margin:8px 0;min-width:0}.relation-source code{font-size:12px;color:#245d9f}.relation-source span{color:#64748b;font-size:12px}.plan-facts{display:grid;grid-template-columns:110px minmax(0,1fr);gap:12px;line-height:1.7;padding-bottom:8px}.plan-facts dt{color:#64748b}.plan-facts dd{margin:0;overflow-wrap:anywhere}.warning-section{border-color:#edcd93;background:#fffaf0}.el-alert{margin:12px 0}code,pre{white-space:pre-wrap;overflow-wrap:anywhere}pre{max-height:280px;overflow:auto}
@media(max-width:700px){.plan-metrics{gap:8px}.plan-metrics>div{padding:10px}.plan-metrics strong{font-size:20px}}
@media(max-width:480px){.plan-metrics{grid-template-columns:minmax(0,1fr)}.plan-metrics>div{display:grid;grid-template-columns:1fr auto;align-items:center}.plan-metrics>div>span:last-child{grid-column:1/-1}.plan-facts{grid-template-columns:minmax(0,1fr);gap:4px}.plan-facts dd{margin-bottom:8px}.plan-action-panel{padding:12px}.batch-actions{width:100%}}
</style>
