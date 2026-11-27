<template>
  <figure class="ocr-code-frame" :class="{ 'has-suggestion': suggestion }">
    <figcaption v-if="label">{{ label }}</figcaption>
    <div class="ocr-code-scroll" tabindex="0" role="region" :aria-label="label || '代码片段'">
      <pre class="ocr-code-gutter" aria-hidden="true">{{ lineNumbers }}</pre>
      <pre class="ocr-code-source"><code>{{ text }}</code></pre>
    </div>
  </figure>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({ text: { type: String, required: true }, label: String, suggestion: Boolean })
const lineNumbers = computed(() => props.text.split(/\r\n|\r|\n/).map((_, index) => index + 1).join('\n'))
</script>

<style scoped>
.ocr-code-frame {
  --code-border: #d8e3f0;
  --code-background: #f4f7fb;
  --code-heading: #eaf0f8;
  --code-label: #344d69;
  --code-gutter: #506780;
  display: flex;
  flex-direction: column;
  min-width: 0;
  margin: 0;
  overflow: hidden;
  border: 1px solid var(--code-border);
  border-radius: 9px;
  background: var(--code-background);
}
.ocr-code-frame.has-suggestion {
  --code-border: #cde7d5;
  --code-background: #f0f9f3;
  --code-heading: #dff1e5;
  --code-label: #255b3a;
  --code-gutter: #466b53;
}
.ocr-code-frame figcaption {
  padding: 7px 10px;
  border-bottom: 1px solid var(--code-border);
  color: var(--code-label);
  background: var(--code-heading);
  font-size: 13px;
  font-weight: 650;
}
.ocr-code-scroll { display: flex; flex: 1; max-height: 264px; min-width: 0; overflow: auto; }
.ocr-code-scroll:focus-visible { outline: 2px solid #2864ba; outline-offset: -2px; }
.ocr-code-frame pre {
  margin: 0;
  padding: 9px 11px;
  border-radius: 0;
  background: transparent;
  font: 12px/1.6 ui-monospace, SFMono-Regular, Consolas, monospace;
  white-space: pre;
  tab-size: 4;
}
.ocr-code-frame .ocr-code-gutter {
  flex: 0 0 auto;
  border-inline-end: 1px solid var(--code-border);
  color: var(--code-gutter);
  background: var(--code-heading);
  text-align: end;
  user-select: none;
}
.ocr-code-source { flex: 1 0 auto; color: #162e49; }
.ocr-code-source code { font: inherit; white-space: inherit; }
</style>
