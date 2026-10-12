// 使用 Vue 渲染生命周期验证单次在途请求、异常与卸载，不依赖浏览器伪造结果。
const { test } = require('node:test')
const assert = require('node:assert/strict')
const { createRenderer, h } = require('vue')
const renderer = createRenderer({ createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {} })
const turn = () => new Promise(resolve => setTimeout(resolve, 10))
test('刷新不重叠，卸载中止请求并拒绝旧响应', async () => {
  global.document = { hidden: false, addEventListener() {}, removeEventListener() {} }
  const { useOperationPolling } = await import('../src/composables/useOperationPolling.js')
  let resolve, count = 0, signal, poll
  const received = []
  const app = renderer.createApp({ setup() {
    poll = useOperationPolling(config => { count++; signal = config.signal; return new Promise(r => { resolve = r }) }, data => received.push(data), () => true)
    return () => null
  } })
  app.mount({}); poll.refresh(); poll.refresh()
  assert.equal(count, 1)
  resolve('第一次'); await turn()
  assert.deepEqual(received, ['第一次']); assert.equal(count, 2)
  app.unmount(); assert.ok(signal.aborted)
  resolve('过期结果'); await turn()
  assert.deepEqual(received, ['第一次'])
  delete global.document
})
test('读取失败显示连接提示，不写入错误业务状态', async () => {
  global.document = { hidden: false, addEventListener() {}, removeEventListener() {} }
  const { useOperationPolling } = await import('../src/composables/useOperationPolling.js')
  let poll, applied = 0
  const app = renderer.createApp({ setup() { poll = useOperationPolling(async () => { throw new Error('断网') }, () => applied++, () => true); return () => null } })
  app.mount({}); await turn()
  assert.match(poll.error.value, /进度刷新失败/); assert.equal(applied, 0)
  app.unmount(); delete global.document
})
