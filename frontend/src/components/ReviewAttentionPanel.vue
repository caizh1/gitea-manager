<template>
  <section v-if="attention.summary?.pr_count" class="glass-card attention-panel" aria-labelledby="review-attention-heading">
    <div class="attention-heading"><div><h4 id="review-attention-heading">需要你处理 <span class="attention-count">{{ attention.summary.pr_count }}</span></h4><p>完成核对后，对应 PR 的后续审查将继续。</p></div><el-button link type="primary" @click="$emit('open')">查看全部待办<el-icon><ArrowRight /></el-icon></el-button></div>
    <div v-for="item in attention.items.slice(0, 3)" :key="`${item.repository}-${item.number}`" class="attention-row">
      <div class="attention-identity"><strong>{{ item.repo_name }} <span>#{{ item.number }}</span></strong><p>前次发布结果待核对<span v-if="item.pending_count > 1"> · {{ item.pending_count }} 项</span></p></div>
      <div class="attention-impact"><el-tag type="danger" effect="light">{{ item.blocked_count ? `阻塞 ${item.blocked_count} 条审查` : '需要核对' }}</el-tag></div>
      <span class="attention-wait">已等待 {{ attentionWait(item.since) }}</span>
      <el-button type="primary" @click="$emit('open', item)">立即处理</el-button>
    </div>
  </section>
</template>
<script setup>
import { ArrowRight } from '@element-plus/icons-vue'
import { useReviewAttention, attentionWait } from '../composables/useReviewAttention'
defineEmits(['open'])
const attention = useReviewAttention()
</script>
<style scoped>
.attention-panel { margin: 0 0 26px; background: rgba(255,255,255,.8); }
.attention-heading { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; padding: 22px 26px; border-bottom: 1px solid rgba(15,23,42,.06); }
.attention-heading h4 { display: flex; align-items: center; gap: 12px; font-size: 21px; margin: 0; }
.attention-count { display: inline-flex; align-items: center; justify-content: center; min-width: 30px; min-height: 30px; padding: 0 8px; background: var(--color-danger); color: white; border-radius: 18px; font-size: 18px; }
.attention-heading p, .attention-identity p { color: var(--text-secondary); margin: 5px 0 0; font-size: 14px; line-height: 1.6; }
.attention-row { display: grid; grid-template-columns: minmax(200px, 1.5fr) minmax(112px,.8fr) minmax(135px,.9fr) auto; align-items: center; gap: 18px; padding: 14px 0; margin: 0 26px; }
.attention-row + .attention-row { border-top: 1px solid rgba(15,23,42,.06); }
.attention-identity { min-width: 0; overflow-wrap: anywhere; }
.attention-identity strong { font-size: 16px; line-height: 1.6; }.attention-identity strong span { white-space: nowrap; }
.attention-wait { color: var(--text-secondary); font-size: 14px; }
.attention-row :deep(.el-button) { min-width: 116px; height: 42px; font-weight: 600; margin: 0; }
@media (max-width: 1000px) { .attention-row { grid-template-columns: minmax(0,1fr) auto; gap: 10px; }.attention-identity { grid-column: 1 / -1; }.attention-impact { grid-column: 1; }.attention-row :deep(.el-button) { grid-column: 2; grid-row: 2 / span 2; }.attention-wait { grid-column: 1; }.attention-heading { padding: 18px; }.attention-row { margin: 0 18px; } }
</style>
