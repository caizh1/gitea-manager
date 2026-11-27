<template>
  <div v-if="props.events.length" class="ocr-event-scroll" tabindex="0" role="region" aria-label="扫描事件记录">
    <div v-for="(items, day) in days" :key="day">
      <h3 class="ocr-event-day">{{ day }}</h3>
      <ol class="ocr-event-list" role="list">
        <li v-for="event in items" :key="event.id" class="ocr-event">
          <span class="ocr-event-rail" aria-hidden="true">
            <span class="ocr-event-marker" :class="'is-' + event.tone">
              <Check v-if="event.tone === 'success'" :size="12" />
              <TriangleAlert v-else-if="event.tone === 'warning'" :size="12" />
            </span>
          </span>
          <div class="ocr-event-copy">
            <div class="ocr-event-header">
              <strong>{{ event.kind }}</strong>
              <time :datetime="event.timestamp.toISOString()">{{ clock.format(event.timestamp) }}</time>
            </div>
            <div v-if="event.detail.http || event.detail.tool || event.detail.file" class="ocr-event-detail">
              <span v-if="event.detail.http" class="ocr-event-http">HTTP {{ event.detail.http }}</span>
              <code v-if="event.detail.tool">{{ event.detail.tool }}</code>
              <code v-if="event.detail.file">{{ event.detail.file }}</code>
            </div>
          </div>
        </li>
      </ol>
    </div>
  </div>
  <p v-else class="ocr-event-empty">暂无事件</p>
</template>

<script setup>
import { computed } from 'vue'
import { Check, TriangleAlert } from '@lucide/vue'

const props = defineProps({ events: { type: Array, required: true } })
const calendar = new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium' })
const clock = new Intl.DateTimeFormat('zh-CN', { timeStyle: 'medium', hourCycle: 'h23' })
const tones = {
  报告已生成: 'success',
  模型连接已恢复: 'success',
  已保存文件检查点: 'checkpoint',
  核对覆盖率并导出报告: 'checkpoint',
  等待恢复: 'warning',
  等待人工处理: 'warning',
}
const days = computed(() => {
  const groups = Object.create(null)
  for (const event of [...props.events].reverse()) {
    const timestamp = new Date(event.at * 1000)
    const day = calendar.format(timestamp)
    const tone = tones[event.kind] || (event.detail.http >= 400 ? 'warning' : 'neutral')
    const items = groups[day] ??= []
    items.push({ ...event, timestamp, tone })
  }
  return groups
})
</script>

<style scoped>
.ocr-event-scroll {
  max-height: 440px;
  min-width: 0;
  overflow: auto;
  scrollbar-gutter: stable;
  scrollbar-width: thin;
  scrollbar-color: #9db4d2 transparent;
}
.ocr-event-scroll:focus-visible { outline: 2px solid #2864ba; outline-offset: 2px; border-radius: 6px; }
.ocr-event-day { margin: 0 0 7px 28px; color: #4a5e78; font-size: 12px; font-weight: 600; }
.ocr-event-list { margin: 0 0 14px; padding: 0 3px; list-style: none; }
.ocr-event-list:last-child { margin-bottom: 0; }
.ocr-event-scroll > div + div { margin-top: 13px; }
.ocr-event { display: grid; grid-template-columns: 20px minmax(0, 1fr); gap: 10px; }
.ocr-event-rail { display: flex; flex-direction: column; align-items: center; }
.ocr-event-rail::after { content: ''; width: 1px; flex: 1; background: #c5d7ec; }
.ocr-event:last-child .ocr-event-rail::after { visibility: hidden; }
.ocr-event-marker {
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  width: 12px;
  height: 12px;
  margin-block: 4px 0;
  border: 2px solid #f5f9fe;
  border-radius: 50%;
  color: #fff;
  background: #748ca7;
}
.ocr-event-marker.is-checkpoint { background: #2769c4; }
.ocr-event-marker.is-success, .ocr-event-marker.is-warning { width: 19px; height: 19px; margin-block-start: 1px; }
.ocr-event-marker.is-success { color: #187244; background: #daefdf; }
.ocr-event-marker.is-warning { color: #8b470d; background: #fce7c9; }
.ocr-event-copy { min-width: 0; padding-bottom: 12px; }
.ocr-event-header { display: flex; gap: 3px 10px; align-items: baseline; justify-content: space-between; flex-wrap: wrap; }
.ocr-event-header strong { min-width: 0; color: #162841; font-size: 14px; font-weight: 650; line-height: 20px; overflow-wrap: anywhere; }
.ocr-event-header time { flex: 0 0 auto; color: #465b76; font-size: 12px; font-variant-numeric: tabular-nums; }
.ocr-event-detail { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 4px; }
.ocr-event-detail > * {
  max-width: 100%;
  padding: 2px 6px;
  border-radius: 5px;
  color: #2d4667;
  background: #e8eff8;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.ocr-event-detail .ocr-event-http { color: #794008; background: #fcecd6; font-weight: 650; }
.ocr-event-empty { margin: 0; padding: 12px 0; color: #465b76; font-size: 13px; }
</style>
