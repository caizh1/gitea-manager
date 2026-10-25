// 使用真实 Axios 拦截器覆盖业务错误、下载、取消与权限切换，不修改后端判定。
const { test, before, after } = require('node:test')
const assert = require('node:assert/strict')
let api, applySession, accessState, notifyApiError, events
before(async () => {
  ;({ api } = await import('../src/api/index.js'))
  ;({ applySession, accessState } = await import('../src/composables/accessState.js'))
  ;({ notifyApiError } = await import('../src/api/errors.js'))
  global.window = new EventTarget()
  events = []
  window.addEventListener('manager-api-error', event => events.push(event.detail))
  applySession({ authenticated: true, user: { id: 1, is_platform_admin: true }, capabilities: ['ai_reviews'] })
})
after(() => { delete global.window })
const failed = (status, data, code) => config => Promise.reject({ config, code, response: status ? { status, data } : undefined })

test('成功响应与二进制下载保持原样，不产生错误提示', async () => {
  const start = events.length
  const data = new Blob(['隔离测试下载内容'])
  const result = await api.get('/ai-review/tasks/1/events/export', { responseType: 'blob', adapter: async config => ({ status: 200, data, config }) })
  assert.equal(result.data, data)
  assert.equal(events.length, start)
})

test('已关闭 PR 重审失败展示原因且仍向调用方拒绝；局部处理不重复提示', async () => {
  const start = events.length
  let caught
  await api.post('/ai-review/repositories/1/rerun', { number: 7 }, { adapter: failed(400, { error: 'PR 已关闭或合并' }) }).catch(error => { caught = error; notifyApiError(error) })
  assert.equal(caught.response.status, 400)
  assert.deepEqual(events.slice(start), ['PR 已关闭或合并'])
})

test('服务器、镜像、定时任务、统计与告警入口均受到公共错误提示保护', async () => {
  for (const [method, url] of [['post', '/servers'], ['post', '/mirrors/1/sync'], ['put', '/schedules/1'], ['get', '/statistics/1/authors'], ['post', '/alerts/1/clear']]) {
    const start = events.length
    await assert.rejects(api.request({ method, url, adapter: failed(500, { error: '服务暂时不可用' }) }))
    assert.deepEqual(events.slice(start), ['服务暂时不可用'])
  }
})

test('二进制下载的 JSON 错误可见且局部仍能读取；非 JSON 显示状态码', async () => {
  const start = events.length
  await assert.rejects(api.get('/ai-review/servers/1/progress-assets', { responseType: 'blob', adapter: failed(403, new Blob(['{"error":"当前账号无权下载"}'])) }), error => error.response.data.error === '当前账号无权下载')
  await assert.rejects(api.get('/ai-review/tasks/1/events/export', { responseType: 'blob', adapter: failed(502, new Blob(['<html>网关不可用</html>'])) }))
  assert.deepEqual(events.slice(start), ['当前账号无权下载', '请求失败（HTTP 502），请刷新核对操作结果'])
})

test('超时、断网和额外错误详情均有明确反馈', async () => {
  const start = events.length
  await assert.rejects(api.get('/servers', { adapter: failed(null, null, 'ECONNABORTED') }))
  await assert.rejects(api.get('/servers', { adapter: failed(null, null, 'ERR_NETWORK') }))
  await assert.rejects(api.post('/commit-rules', {}, { adapter: failed(400, { error: '保存失败', detail: '规则表达式无效' }) }))
  assert.deepEqual(events.slice(start), ['请求超时，请刷新核对操作结果', '网络连接失败，请检查连接并刷新核对操作结果', '保存失败：规则表达式无效'])
})

test('后台轮询、请求取消和会话探测不弹窗，保留原有拒绝语义', async () => {
  const start = events.length
  await assert.rejects(api.get('/ai-review/tasks', { notifyError: false, adapter: failed(503, { error: '后台读取失败' }) }))
  await assert.rejects(api.get('/servers', { adapter: failed(null, null, 'ERR_CANCELED') }))
  for (const url of ['/session', '/login', '/access/directory/test-login']) await assert.rejects(api.post(url, {}, { adapter: failed(401, { error: '身份认证失败' }) }))
  await assert.rejects(api.get('/servers', { adapter: failed(401, { error: '会话已失效' }) }))
  assert.equal(events.length, start)
})

test('解码下载错误期间切换权限，不泄漏旧作用域原因', async () => {
  const start = events.length
  class DelayedBlob extends Blob {
    async text() { accessState.generation++; return '{"error":"旧组织的内部错误"}' }
  }
  await assert.rejects(api.get('/ai-review/servers/1/progress-assets', { responseType: 'blob', adapter: failed(403, new DelayedBlob(['错误'])) }), error => error.code === 'ERR_CANCELED')
  assert.equal(events.length, start)
})
