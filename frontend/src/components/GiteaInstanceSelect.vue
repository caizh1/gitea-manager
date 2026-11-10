<template>
  <div class="gitea-instance-field">
    <el-select :model-value="modelValue" :aria-label="ariaLabel" :disabled="disabled" placeholder="选择已登记的 Gitea 实例" filterable fit-input-width @update:model-value="emit('update:modelValue', $event)" @change="emit('change', $event)">
      <el-option v-for="server in servers" :key="server.id" :value="server.id" :label="serverLabel(server)" class="gitea-instance-option">
        <span class="gitea-instance-heading">{{ serverRoleLabel(server.role) }} · {{ server.name || `实例 #${server.id}` }}</span>
        <small>{{ serverAddress(server) || '地址未配置' }}</small>
      </el-option>
    </el-select>
    <p v-if="selected" class="gitea-instance-summary">{{ serverLabel(selected) }}</p>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { serverAddress, serverLabel, serverRoleLabel } from './serverDisplay.js'

const props = defineProps({
  modelValue: { type: Number, default: null },
  servers: { type: Array, default: () => [] },
  ariaLabel: { type: String, default: '目标 Gitea 实例' },
  disabled: Boolean,
})
const emit = defineEmits(['update:modelValue', 'change'])
const selected = computed(() => props.servers.find(server => server.id === props.modelValue))
</script>

<style scoped>
.gitea-instance-field { width: 100%; min-width: 0; }
.gitea-instance-field .el-select { width: 100%; min-width: 0; }
.gitea-instance-option { height: auto; min-height: 54px; padding: 8px 20px; line-height: 1.5; white-space: normal; overflow-wrap: anywhere; }
.gitea-instance-heading { display: block; }
.gitea-instance-option small { display: block; color: var(--el-text-color-secondary); font-size: 12px; font-weight: normal; }
.gitea-instance-summary { margin: 6px 0 0; color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.6; overflow-wrap: anywhere; }
</style>
