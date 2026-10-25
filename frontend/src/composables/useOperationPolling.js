import { ref, onMounted, onUnmounted } from 'vue'

// 只读刷新串行执行；卸载和超时后的响应不能再写入页面。
export function useOperationPolling(read, apply, active) {
  const error = ref('')
  let stopped = false, busy = false, again = false, timer, controller
  async function refresh() {
    if (stopped) return
    if (busy) { again = true; return }
    clearTimeout(timer)
    busy = true
    controller = new AbortController()
    const current = controller
    try {
      const result = await read({ signal: current.signal, timeout: 8000, notifyError: false })
      if (!stopped && !current.signal.aborted) { apply(result); error.value = '' }
    } catch (e) {
      if (!stopped) error.value = '进度刷新失败，保留最后记录；正在等待连接恢复。'
    } finally {
      busy = false
      if (!stopped) {
        const delay = again ? 0 : document.hidden || !active() ? 10000 : 2000
        again = false
        timer = setTimeout(refresh, delay)
      }
    }
  }
  function visible() { if (!document.hidden) refresh() }
  onMounted(() => { document.addEventListener('visibilitychange', visible); refresh() })
  onUnmounted(() => { stopped = true; clearTimeout(timer); controller?.abort(); document.removeEventListener('visibilitychange', visible) })
  return { refresh, error }
}
