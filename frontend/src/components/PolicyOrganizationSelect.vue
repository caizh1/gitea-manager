<template>
  <el-select :model-value="modelValue?.id" filterable remote clearable :remote-method="search.search" :loading="state.loading"
    :disabled="disabled || !serverId" placeholder="搜索并选择组织" aria-label="选择组织" popper-class="policy-identity-menu"
    @visible-change="visible => visible && search.open()" @update:model-value="select">
    <el-option v-for="org in options" :key="org.id" :value="org.id" :label="org.name" />
    <template v-if="state.error || state.hasMore" #footer><div class="organization-actions">
      <span v-if="state.error" role="alert">{{ state.error }}</span>
      <el-button v-if="state.error" link @click.stop="search.retry">重试查询</el-button>
      <el-button v-else link :loading="state.loading" @click.stop="search.more">加载更多组织</el-button>
    </div></template>
  </el-select>
</template>
<script setup>
import { computed, onUnmounted, reactive, watch } from 'vue'
import { api } from '../api'
import { createCandidateSearch } from './policyForm'
const props = defineProps({ modelValue: Object, serverId: Number, disabled: Boolean })
const emit = defineEmits(['update:modelValue'])
const state = reactive({})
const search = createCandidateSearch({ state, request: async (context, q, page, signal) => (await api.get('/repository-policies/organizations', { params: { server_id: context.serverId, q, page }, signal })).data })
watch(() => [props.serverId, props.disabled], () => search.reset(props.serverId && !props.disabled ? { serverId: props.serverId } : null), { immediate: true })
const options = computed(() => { const values = new Map(props.modelValue ? [[props.modelValue.id, props.modelValue]] : []); state.items.forEach(x => values.set(x.id, x)); return [...values.values()] })
function select(id) { emit('update:modelValue', options.value.find(x => x.id === id) || null) }
onUnmounted(search.dispose)
</script>
<style scoped>
.organization-actions { display: flex; gap: 8px; flex-wrap: wrap; overflow-wrap: anywhere; }
</style>
