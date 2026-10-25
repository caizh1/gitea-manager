<template>
  <section class="review-chart" :aria-label="title">
    <h4>{{ title }}</h4>
    <div class="chart-legend"><span v-for="s in series" :key="s.key"><i :style="{ background: s.color }" />{{ s.label }}</span></div>
    <canvas ref="canvas" :aria-label="title + '；精确数值可在下方数据表查看'" role="img" @mousemove="inspect" @mouseleave="hint = ''" />
    <p class="chart-hint" aria-live="polite">{{ hint || description }}</p>
    <details><summary>查看数据表</summary><div class="data-scroll"><table><thead><tr><th scope="col">{{ labelTitle }}</th><th v-for="s in series" :key="s.key" scope="col">{{ s.label }}</th></tr></thead><tbody><tr v-for="r in rows" :key="r.label"><th scope="row">{{ r.label }}</th><td v-for="s in series" :key="s.key">{{ r[s.key] ?? 0 }}</td></tr></tbody></table></div></details>
  </section>
</template>
<script setup>
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
const props = defineProps({ title: String, description: String, rows: { type: Array, default: () => [] }, series: { type: Array, default: () => [] }, labelTitle: { type: String, default: '日期' } })
const canvas = ref(), hint = ref('')
let observer, plot = null
function draw() {
  const el = canvas.value
  if (!el) return
  const w = el.clientWidth, h = 132, scale = window.devicePixelRatio || 1
  if (!w) return
  el.width = Math.round(w * scale); el.height = Math.round(h * scale)
  const ctx = el.getContext('2d'); ctx.scale(scale, scale)
  const left = 30, right = 8, top = 12, bottom = 28, width = w - left - right, height = h - top - bottom
  const maximum = Math.max(1, ...props.rows.map(r => props.series.reduce((n, s) => n + (r[s.key] || 0), 0)))
  ctx.font = '11px system-ui'; ctx.textBaseline = 'middle'
  const ticks = Math.min(4, maximum)
  for (let i = 0; i <= ticks; i++) {
    const value = Math.ceil(maximum / ticks * i), y = top + height * (1 - value / maximum)
    ctx.strokeStyle = '#e6ecf3'; ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(w - right, y); ctx.stroke()
    ctx.fillStyle = '#65758b'; ctx.textAlign = 'right'; ctx.fillText(String(value), left - 6, y)
  }
  const step = width / Math.max(1, props.rows.length), bar = Math.max(1, Math.min(32, step * .66))
  const every = Math.max(1, Math.ceil(props.rows.length * 50 / width))
  props.rows.forEach((r, i) => {
    let sum = 0
    for (const s of props.series) {
      const value = r[s.key] || 0, bh = value / maximum * height
      ctx.fillStyle = s.color; ctx.fillRect(left + step * (i + .5) - bar / 2, top + height - (sum + value) / maximum * height, bar, bh)
      sum += value
    }
    if (i % every === 0) {
      ctx.textAlign = 'center'; ctx.fillStyle = '#65758b'
      const label = /^\d{4}-/.test(r.label) ? r.label.slice(5) : r.label
      ctx.fillText(label, left + step * (i + .5), h - 10, Math.max(30, step * every))
    }
  })
  plot = { left, width, step }
}
function inspect(event) {
  if (!plot) return
  const index = Math.floor((event.clientX - canvas.value.getBoundingClientRect().left - plot.left) / plot.step)
  const row = props.rows[index]
  hint.value = row ? `${row.label}：` + props.series.map(s => `${s.label} ${row[s.key] || 0}`).join(' · ') : ''
}
watch(() => [props.rows, props.series], draw, { flush: 'post' })
onMounted(() => { observer = new ResizeObserver(draw); observer.observe(canvas.value); draw() })
onBeforeUnmount(() => observer?.disconnect())
</script>
<style scoped>
.review-chart { min-width: 0; padding: 12px 14px; border: 1px solid #e3eaf2; border-radius: 14px; background: rgba(255,255,255,.65); }
h4 { margin: 0 0 6px; font-size: 14px; }
canvas { display: block; width: 100%; height: 132px; }
.chart-legend { display: flex; gap: 12px; flex-wrap: wrap; color: #52637b; font-size: 12px; min-height: 24px; }
.chart-legend span { display: inline-flex; align-items: center; gap: 5px; }.chart-legend i { width: 8px; height: 8px; border-radius: 2px; }
.chart-hint { min-height: 18px; font-size: 12px; line-height: 1.5; color: #52637b; margin: 6px 0; }
summary { cursor: pointer; color: #275d9b; font-size: 12px; padding: 4px 0; }
.data-scroll { max-height: 250px; overflow: auto; }table { border-collapse: collapse; width: 100%; font-size: 12px; }th,td { padding: 8px 5px; text-align: right; border-bottom: 1px solid #e3eaf2; }th:first-child { text-align: left; }
</style>
