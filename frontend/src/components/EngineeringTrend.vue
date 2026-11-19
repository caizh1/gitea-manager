<template>
  <div>
    <div class="trend-legend">
      <span v-for="(label, index) in labels" :key="label.key">
        <i :style="{ backgroundColor: colors[index] }"></i>{{ label.label }}
      </span>
    </div>
    <canvas ref="canvas" tabindex="0" role="img" aria-label="每日趋势。左右方向键选择日期，回车查看明细"
      @pointermove="point" @click="selectPoint" @keydown.left.prevent="move(-1)" @keydown.right.prevent="move(1)"
      @keydown.enter.prevent="open" />
    <div class="trend-footer">
      <span v-if="selected">{{ selected.day }} · {{ (incomplete(selected) ? '采集有缺口 · ' : '') + labels.map(label => label.label + ' ' + selected[label.key].toLocaleString('zh-CN')).join(' · ') }}</span>
      <el-button v-if="selected" size="small" @click="open">查看当日明细</el-button>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
const props = defineProps({ series: { type: Array, required: true }, labels: { type: Array, required: true } })
const emit = defineEmits(['select'])
const colors = ['#2864d5', '#059889']
const canvas = ref(null)
const index = ref(null)
const selected = computed(() => props.series[index.value ?? props.series.length - 1])
const codeMetric = computed(() => ['additions', 'commits'].includes(props.labels[0]?.key))
const incomplete = row => row.coverage.roster || Boolean(row.coverage[codeMetric.value ? 'code' : 'events'])
let observer
let plot = { left: 48, right: 48 }

function draw() {
  const element = canvas.value
  if (!element) return
  const width = element.clientWidth
  const height = element.clientHeight
  if (!width || !height) return
  const ratio = window.devicePixelRatio || 1
  element.width = Math.round(width * ratio)
  element.height = Math.round(height * ratio)
  const ctx = element.getContext('2d')
  ctx.scale(ratio, ratio)
  const left = 48
  const right = Math.max(left + 1, width - 19)
  const top = 16
  const bottom = height - 27
  plot = { left, right }
  const rows = props.series
  const peak = Math.max(1, ...rows.flatMap(row => props.labels.map(label => row[label.key])))
  const unit = Math.pow(10, Math.floor(Math.log10(peak)))
  const max = Math.ceil(peak / unit) * unit
  const x = i => rows.length === 1 ? (left + right) / 2 : left + i * (right - left) / Math.max(rows.length - 1, 1)
  const y = value => bottom - value * (bottom - top) / max
  ctx.font = '11px system-ui'
  ctx.fillStyle = '#566882'
  ctx.textAlign = 'right'
  for (let i = 0; i <= 4; i++) {
    const value = max * i / 4
    const level = y(value)
    ctx.strokeStyle = '#e4ebf5'
    ctx.beginPath()
    ctx.moveTo(left, level)
    ctx.lineTo(right, level)
    ctx.stroke()
    ctx.fillText(Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 }).format(value), left - 7, level + 4)
  }
  const step = Math.max(1, Math.ceil(rows.length / Math.max(2, Math.floor(width / 82))))
  rows.forEach((row, i) => {
    if (i % step === 0) {
      ctx.textAlign = 'center'
      ctx.fillText(row.day.slice(5), x(i), bottom + 19)
    }
  })
  props.labels.forEach((label, color) => {
    ctx.strokeStyle = colors[color]
    ctx.fillStyle = colors[color]
    ctx.lineWidth = 2
    ctx.beginPath()
    let connected = false
    rows.forEach((row, i) => {
      if (incomplete(row)) { connected = false; return }
      if (connected) ctx.lineTo(x(i), y(row[label.key]))
      else ctx.moveTo(x(i), y(row[label.key]))
      connected = true
    })
    ctx.stroke()
    rows.forEach((row, i) => {
      if (incomplete(row) && !row[label.key]) return
      ctx.beginPath()
      ctx.arc(x(i), y(row[label.key]), rows.length > 50 ? 2 : 3, 0, 2 * Math.PI)
      if (incomplete(row)) ctx.stroke()
      else ctx.fill()
    })
  })
  if (index.value !== null && rows[index.value]) {
    ctx.strokeStyle = '#98acc8'
    ctx.lineWidth = 1
    ctx.setLineDash([3, 4])
    ctx.beginPath()
    ctx.moveTo(x(index.value), top)
    ctx.lineTo(x(index.value), bottom)
    ctx.stroke()
  }
}
function point(event) {
  const raw = (event.offsetX - plot.left) / (plot.right - plot.left)
  if (props.series.length) index.value = Math.max(0, Math.min(props.series.length - 1, Math.round(raw * (props.series.length - 1))))
}
function selectPoint(event) { point(event); open() }
function move(by) {
  if (props.series.length) index.value = Math.max(0, Math.min(props.series.length - 1, (index.value ?? 0) + by))
}
function open() { if (selected.value) emit('select', selected.value.day) }
watch(() => [props.series, props.labels], () => { index.value = null; draw() }, { flush: 'post' })
watch(index, draw)
onMounted(() => { observer = new ResizeObserver(draw); observer.observe(canvas.value) })
onUnmounted(() => observer?.disconnect())
</script>

<style scoped>
.trend-legend, .trend-footer { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 18px; color: #4b5d75; font-size: 12px; }
.trend-legend span { display: inline-flex; align-items: center; gap: 6px; }
.trend-legend i { width: 9px; height: 9px; border-radius: 50%; flex: 0 0 9px; }
canvas { display: block; width: 100%; height: 235px; margin: 9px 0 0; cursor: crosshair; border-radius: 6px; }
.trend-footer { justify-content: space-between; min-height: 30px; }
</style>
