<template>
  <section v-if="count || attention.error" class="review-attention-banner" :class="{ 'is-unavailable': !count }" aria-label="Review 紧急待办">
    <el-icon :size="34" class="attention-icon"><WarnTriangleFilled /></el-icon>
    <div class="attention-copy" role="status" aria-live="polite">
      <strong v-if="count">{{ attention.summary.blocked_task_count ? `${count} 个 PR 需要处理，${attention.summary.blocked_task_count} 条 Review 正在等待` : `${count} 个 PR 的发布结果需要核对` }}</strong>
      <strong v-else>Review 待办状态暂时无法读取</strong>
      <p v-if="count">{{ attention.summary.blocked_task_count ? '前次发布结果尚未确认，后续审查暂时无法开始' : '请核对前次发布结果，处理后提醒将自动移除' }}</p>
      <p v-if="attention.error" class="attention-error">{{ attention.error }}{{ attention.loaded ? '；当前保留上次读取的待办' : '' }}</p>
    </div>
    <el-button v-if="count" type="danger" size="large" @click="router.push(attentionRoute())">立即处理<el-icon><Right /></el-icon></el-button>
    <el-button v-else @click="refreshReviewAttention(true)">重新读取</el-button>
  </section>
</template>
<script setup>
import { computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { WarnTriangleFilled, Right } from '@element-plus/icons-vue'
import { useReviewAttention, attentionRoute, refreshReviewAttention, startReviewAttention, stopReviewAttention } from '../composables/useReviewAttention'
const router = useRouter(), attention = useReviewAttention()
const count = computed(() => attention.summary?.pr_count || 0)
onMounted(startReviewAttention)
onUnmounted(stopReviewAttention)
</script>
<style scoped>
.review-attention-banner { display: flex; align-items: center; gap: 22px; flex-wrap: wrap; flex-shrink: 0; margin: 20px 28px 0; padding: 20px 24px; border: 1px solid #f3c5c5; border-radius: 16px; background: linear-gradient(110deg, #fff2f3, #fff7f7); color: #9e2424; }
.attention-icon { flex: 0 0 38px; }
.attention-copy { flex: 1 1 320px; min-width: 0; overflow-wrap: anywhere; }
.attention-copy strong { font-size: 20px; line-height: 1.6; }
.attention-copy p { margin: 4px 0 0; color: var(--text-secondary); font-size: 14px; line-height: 1.6; }
.attention-copy .attention-error { color: #9e2424; }
.review-attention-banner :deep(.el-button) { margin: 0; gap: 10px; flex-shrink: 0; font-weight: 600; }
.is-unavailable { background: #fff8ee; color: #754200; border-color: #ead2b2; }
@media (max-width: 720px) { .review-attention-banner { margin: 12px 12px 0; padding: 14px; gap: 10px; } .attention-copy { flex-basis: 200px; } .attention-copy strong { font-size: 15px; } .attention-copy p { font-size: 13px; } .review-attention-banner :deep(.el-button) { margin-left: auto; } }
</style>
