<script>
import { h } from 'vue'
import OcrReportCode from './OcrReportCode.vue'

const inline = parts => parts.map(([style, value]) =>
  style === 'code' || style === 'strong' ? h(style, value) : value
)

export default {
  props: { blocks: Array, raw: String },
  setup(props) {
    return () => h('div', { class: 'ocr-report-text' }, props.blocks?.length
      ? props.blocks.map(block => {
        if (block.kind === 'code') return h(OcrReportCode, { text: block.text })
        if (block.kind === 'ordered' || block.kind === 'unordered') {
          return h(block.kind === 'ordered' ? 'ol' : 'ul', block.items.map(value => h('li', inline(value))))
        }
        return h(block.kind === 'heading' ? 'h3' : 'p', inline(block.text))
      })
      : h('p', props.raw))
  },
}
</script>

<style scoped>
.ocr-report-text { overflow-wrap: anywhere; line-height: 1.6; }
.ocr-report-text :deep(h3) { font-size: 15px; }
.ocr-report-text :deep(p), .ocr-report-text :deep(h3) { margin: 8px 0; }
.ocr-report-text :deep(ul), .ocr-report-text :deep(ol) { margin: 7px 0; padding-inline-start: 22px; }
.ocr-report-text :deep(p code), .ocr-report-text :deep(li code) { padding: 1px 4px; background: #edf3fb; border-radius: 4px; }
</style>
