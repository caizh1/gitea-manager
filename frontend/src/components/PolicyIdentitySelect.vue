<template>
  <div class="policy-identity">
    <el-select :model-value="modelValue" multiple filterable remote :remote-method="search.search" :reserve-keyword="false" popper-class="policy-identity-menu"
      :disabled="disabled || !available" :loading="state.loading" :aria-label="label" :placeholder="placeholder"
      :no-data-text="state.error ? '查询失败，已选名单保留' : '没有匹配的候选项'" @update:model-value="select"
      @visible-change="visible => visible && search.open()">
      <el-option v-for="item in options" :key="item.name" :label="item.label" :value="item.name" />
      <template v-if="state.hasMore || state.error" #footer><div class="identity-actions">
      <span v-if="state.error" role="alert" class="identity-error">{{ state.error }}</span>
      <el-button v-if="state.error" link type="primary" :disabled="disabled || !available || state.loading" @click.stop="search.retry">重试查询</el-button>
      <el-button v-else-if="state.hasMore" link type="primary" :loading="state.loading" :disabled="disabled" @click.stop="search.more">加载更多候选项</el-button>
      </div></template>
    </el-select>
  </div>
</template>

<script setup>
import { computed, onUnmounted, reactive, watch } from 'vue'
import { api } from '../api'
import { createCandidateSearch } from './policyForm'
const props = defineProps({ modelValue: { type: Array, default: () => [] }, serverId: Number, organization: Object, identities: Object,
  kind: { type: String, required: true }, label: { type: String, required: true }, disabled: Boolean })
const emit = defineEmits(['update:modelValue'])
const state = reactive({ items: [], query: '', page: 0, hasMore: false, loading: false, error: '', loaded: false })
const available = computed(() => props.serverId && (props.kind === 'users' || props.organization))
const placeholder = computed(() => props.kind === 'users' ? '搜索用户…' : '搜索团队…')
const search = createCandidateSearch({ state, request: async (context, query, page, signal) => (await api.get('/repository-policies/' + context.kind,
  { params: { server_id: context.serverId, org: context.organization?.name, org_id: context.organization?.id, q: query, page }, signal })).data })
watch(() => [props.serverId, props.kind, props.organization?.id, props.organization?.name, props.disabled], () => search.reset(available.value && !props.disabled ?
  { serverId: props.serverId, kind: props.kind, organization: props.organization } : null), { immediate: true })
// 所选名称不依赖当前分页或查询成功；避免 Element Plus 将未加载的旧值显示成空白。
const options = computed(() => {
  const values = new Map(props.modelValue.map(name => [name, { name, label: props.kind === 'teams' ? `${props.organization?.name || ''} / ${name}` : name }]))
  state.items.forEach(item => values.set(item.name, item))
  return [...values.values()]
})
function select(names) {
  // 只绑定新选值，候选搜索不得把已有同名旧身份替换掉。
  if (props.identities) {
    props.identities[props.kind] ||= []
    for (const name of names.filter(name => !props.modelValue.includes(name))) {
      const candidate = state.items.find(x => x.name === name)
      if (candidate) {
        props.identities[props.kind] = props.identities[props.kind].filter(x => x.name.toLowerCase() !== name.toLowerCase())
        props.identities[props.kind].push({ ...candidate })
      }
    }
  }
  emit('update:modelValue', names)
}
onUnmounted(search.dispose)
</script>

<style scoped>
.policy-identity, .policy-identity :deep(.el-select) { width: 100%; min-width: 0; }
.identity-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 6px; }
.identity-error { color: var(--el-color-danger); font-size: 13px; overflow-wrap: anywhere; }
.identity-help { color: var(--el-text-color-secondary); font-size: 13px; margin: 4px 0; }
.policy-identity :deep(.el-select__selected-item) { max-width: 100%; }
.policy-identity :deep(.el-tag__content) { overflow-wrap: anywhere; white-space: normal; }
.policy-identity :deep(.el-tag) { height: auto; min-height: 24px; }
/* 候选与分页按钮合计低于半屏，输入框位于窗口中部时也能完整展开。 */
:global(.policy-identity-menu .el-select-dropdown__wrap) { max-height: max(80px, min(240px, calc(50vh - 100px))); }
</style>
