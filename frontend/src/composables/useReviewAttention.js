import { reactive } from 'vue'
import { api } from '../api'

// 全局与页面共享同一份待办；登录会话结束后清空，旧响应不能重新填入。
const state = reactive({ summary: null, items: [], worker: null, loaded: false, error: '', updatedAt: null })
let consumers = 0, timer = null, pending = null, epoch = 0

export async function refreshReviewAttention(force = false) {
  const current = epoch
  if (pending) {
    await pending
    if (!force || current !== epoch) return
  }
  const request = (async () => {
    try {
      const { data } = await api.get('/ai-review/attention', { timeout: 5000 })
      if (current !== epoch) return
      Object.assign(state, { summary: data.summary, items: data.items, worker: data.worker,
        loaded: true, error: '', updatedAt: Date.now() })
    } catch (error) {
      if (current === epoch) state.error = error.response?.data?.error || 'Review 待办状态暂时无法更新'
    }
  })()
  pending = request
  try { await request } finally { if (pending === request) pending = null }
}

async function tick() {
  const current = epoch
  if (!document.hidden) await refreshReviewAttention()
  if (consumers && current === epoch) timer = setTimeout(tick, 5000)
}
function onVisible() { if (!document.hidden) refreshReviewAttention(true) }
export function startReviewAttention() {
  if (++consumers !== 1) return
  document.addEventListener('visibilitychange', onVisible)
  tick()
}
export function stopReviewAttention() {
  if (--consumers > 0) return
  consumers = 0; epoch++; clearTimeout(timer)
  pending = null
  document.removeEventListener('visibilitychange', onVisible)
  Object.assign(state, { summary: null, items: [], worker: null, loaded: false, error: '', updatedAt: null })
}
export function attentionRoute(item) {
  return { path: '/ai-reviews', query: item ? { attention: '1', repository: String(item.repository), number: String(item.number) } : { attention: '1' } }
}
export function attentionWait(since) {
  const minutes = Math.max(0, Math.floor((Date.now() / 1000 - since) / 60))
  if (!since || minutes < 1) return '不足 1 分钟'
  if (minutes < 60) return `${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  return hours < 24 ? `${hours} 小时 ${minutes % 60} 分钟` : `${Math.floor(hours / 24)} 天 ${hours % 24} 小时`
}
export function useReviewAttention() { return state }
