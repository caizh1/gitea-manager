// 通过真实 Axios 请求链验证身份与组织切换，旧作用域响应不能填回新页面。
const { test } = require('node:test')
const assert = require('node:assert/strict')
const load = async () => ({ ...(await import('../src/composables/accessState.js')), ...(await import('../src/api/index.js')) })
const organization = (id, serverId = id) => ({ binding_id: id, server_id: serverId, server_name: `实例 ${serverId}`, organization_id: id + 100, organization_name: `组织 ${id}` })
const session = (extra = {}) => ({ authenticated: true, user: { id: 7, username: 'tester', is_platform_admin: false }, capabilities: ['dashboard', 'repository_policies'], organizations: [organization(1), organization(2)], csrf_token: '测试会话令牌', ...extra })

test('组织绑定 ID 独立于 Gitea ID；默认选择和撤权后重新绑定', async () => {
  const { applySession, accessState, currentOrganization, setOrganization, scopedServers, hasCapability } = await load()
  applySession(session())
  assert.equal(accessState.organizationId, 1)
  assert.equal(currentOrganization.value.organization_id, 101)
  assert.equal(hasCapability('servers'), false)
  assert.deepEqual(scopedServers([{ id: 1 }, { id: 2 }]), [{ id: 1 }])
  const before = accessState.generation
  assert.equal(setOrganization(999), false)
  assert.equal(accessState.generation, before)
  assert.equal(setOrganization(2), true)
  applySession(session({ organizations: [organization(1)] }))
  assert.equal(accessState.organizationId, 1)
  applySession({ authenticated: false })
  assert.equal(accessState.organizationId, null)
  assert.equal(accessState.user, null)
  assert.equal(accessState.csrfToken, '')
})

test('未授权和必须改密码账号不得进入业务界面，平台账号不携带组织范围', async () => {
  const { applySession, accessReady, accessState, scopedServers } = await load()
  applySession(session({ organizations: [] }))
  assert.equal(accessReady.value, false)
  applySession(session({ user: { id: 1, is_platform_admin: true, must_change_password: true } }))
  assert.equal(accessReady.value, false)
  applySession(session({ user: { id: 1, is_platform_admin: true } }))
  assert.equal(accessReady.value, true)
  assert.equal(accessState.organizationId, null)
  assert.deepEqual(scopedServers([{ id: 1 }, { id: 2 }]), [{ id: 1 }, { id: 2 }])
})

test('所有接口统一携带 CSRF 和绑定 ID，独立取消信号仍可工作', async () => {
  const { api, applySession, cancelScopeRequests } = await load()
  applySession(session())
  let captured
  const adapter = async config => { captured = config; return { data: {}, status: 200, config } }
  await api.post('/commit-gates/apply', {}, { adapter })
  assert.equal(captured.headers.get('X-CSRF-Token'), '测试会话令牌')
  assert.equal(captured.headers.get('X-Organization-ID'), '1')
  const controller = new AbortController()
  let release
  const pending = api.get('/ai-review/tasks/1', { signal: controller.signal, adapter: config => new Promise(resolve => { captured = config; release = () => resolve({ data: {}, status: 200, config }) }) })
  await new Promise(resolve => setImmediate(resolve))
  controller.abort()
  assert.equal(captured.signal.aborted, true)
  release()
  await assert.rejects(pending, error => error.code === 'ERR_CANCELED')
  cancelScopeRequests()
})

test('切换组织立即取消在途请求；即使传输已完成旧响应也不能交付', async () => {
  const { api, applySession, setOrganization, cancelScopeRequests } = await load()
  applySession(session())
  let finish, signal
  const pending = api.get('/repository-policies/batches', { adapter: config => new Promise(resolve => { signal = config.signal; finish = () => resolve({ data: { secret: '旧组织' }, status: 200, config }) }) })
  await new Promise(resolve => setImmediate(resolve))
  setOrganization(2); cancelScopeRequests()
  assert.equal(signal.aborted, true)
  finish()
  await assert.rejects(pending, error => error.code === 'ERR_CANCELED')
  const response = api.get('/statistics/2/overview', { adapter: async config => { setOrganization(1); return { data: { secret: '过期结果' }, status: 200, config } } })
  await assert.rejects(response, error => error.code === 'ERR_CANCELED')
})

test('目录测试失败不注销平台会话，业务会话失效广播卸载事件', async () => {
  const { api, applySession } = await load()
  applySession(session())
  let expired = 0
  global.window = { dispatchEvent: () => { expired++ } }
  const adapter = config => Promise.reject({ config, response: { status: 401 } })
  await assert.rejects(api.post('/access/directory/test-login', {}, { adapter }))
  assert.equal(expired, 0)
  await assert.rejects(api.get('/repository-policies/templates', { adapter }))
  assert.equal(expired, 1)
  delete global.window
})
