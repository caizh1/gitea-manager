<template>
  <div class="row-actions">
    <template v-if="!row.access_archived">
      <el-button link type="primary" @click="$emit('command', 'configure', row)">配置</el-button>
      <el-dropdown trigger="click" placement="bottom-end" @command="command => $emit('command', command, row)">
        <el-button class="more-button" :aria-label="`${row.repo_name} 的更多操作`"><EllipsisVertical :size="18" /></el-button>
        <template #dropdown><el-dropdown-menu>
          <el-dropdown-item command="review" :disabled="!row.enabled || !enabled"><CodeXml :size="16" />审查 PR</el-dropdown-item>
          <el-dropdown-item v-if="isPlatformAdmin" command="webhook"><Bell :size="16" />事件通知</el-dropdown-item>
          <el-dropdown-item v-if="isPlatformAdmin" command="download"><Download :size="16" />下载进度组件</el-dropdown-item>
          <el-dropdown-item v-if="isPlatformAdmin" command="remove" divided class="remove-menu-item"><Trash2 :size="16" />移除接入</el-dropdown-item>
        </el-dropdown-menu></template>
      </el-dropdown>
    </template>
    <el-button v-else-if="row.removal_pending && isPlatformAdmin" link type="warning" @click="$emit('command', 'remove', row)">重试移除</el-button>
    <span v-else class="history-only">历史记录</span>
  </div>
</template>

<script setup>
import { Bell, CodeXml, Download, EllipsisVertical, Trash2 } from '@lucide/vue'
import { isPlatformAdmin } from '../composables/accessState'
defineProps({ row: { type: Object, required: true }, enabled: Boolean })
defineEmits(['command'])
</script>

<style scoped>
.row-actions { display: flex; align-items: center; gap: 16px; flex-shrink: 0; }
.more-button { width: 34px; height: 36px; padding: 0; flex: 0 0 34px; background: #f8fbff; color: #1765be; }
.history-only { font-size: 12px; color: #7b899f; }
:global(.remove-menu-item) { color: #d43838 !important; }
:global(.el-dropdown-menu__item > svg) { flex-shrink: 0; margin-right: 10px; }
</style>
