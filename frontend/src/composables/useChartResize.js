import { watch, onUnmounted } from 'vue'

// 监听图表容器，兼顾窗口变化、侧栏折叠和异步数据显示。
export function useChartResize(container, render) {
  let observer
  let frame
  watch(container, element => {
    observer?.disconnect()
    if (!element) return
    let width = -1
    observer = new ResizeObserver(entries => {
      const next = Math.round(entries[0].contentRect.width)
      if (next <= 0 || next === width) return
      width = next
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(render)
    })
    observer.observe(element)
  }, { flush: 'post' })
  onUnmounted(() => { observer?.disconnect(); cancelAnimationFrame(frame) })
}
