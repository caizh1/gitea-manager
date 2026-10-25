// 对生产构建运行真实浏览器点击，以隔离 HTTP 故障注入验证错误可见性。
// 依赖准备机已有的 Playwright；不访问用户的 Gitea、数据库或模型。
const assert = require('node:assert/strict')
const http = require('node:http')
const fs = require('node:fs')
const path = require('node:path')
const { chromium } = require('playwright')
const root = path.resolve(__dirname, '..')
const output = path.join(root, 'work/api-error-ui')
fs.mkdirSync(output, { recursive: true })
const now = Math.floor(Date.now() / 1000)
const servers = [{ id: 1, name: '隔离测试服务器', role: 'primary', host: '127.0.0.1', status: 'online', gitea_port: 3000 }]
const task = { id: 'test-task', repository: 1, number: 7, status: 'superseded', created: now, updated: now, stage: '版本已过期', progress: {}, result: null }
const repositories = [{ id: 1, server_id: 1, server_name: '隔离测试服务器', repo_name: '测试组织/测试仓库', enabled: true }]
const session = { authenticated: true, user: { id: 1, username: '隔离测试管理员', is_platform_admin: true }, csrf_token: '隔离测试令牌', organizations: [], capabilities: ['dashboard', 'servers', 'backups', 'restore', 'mirrors', 'statistics', 'schedules', 'settings', 'ai_reviews', 'commit_gates'] }
const requests = [], results = []
let failPolling = false, rerunAccepted = false
function json(response, data, status = 200) { response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' }); response.end(JSON.stringify(data)) }
const server = http.createServer((request, response) => {
  const url = new URL(request.url, 'http://localhost'), p = url.pathname
  if (p.startsWith('/api/')) {
    requests.push({ method: request.method, path: p })
    if (p === '/api/session') return json(response, session)
    if (p === '/api/ai-review/repositories/1/rerun') return rerunAccepted ? json(response, { job: 'new-test-task' }, 202) : json(response, { error: 'PR 已关闭或合并' }, 400)
    if (p === '/api/servers/1/delete-info') return json(response, { error: '删除影响检查失败，已停止删除' }, 503)
    if (request.method !== 'GET') return json(response, { error: '隔离验证：操作被服务拒绝' }, 409)
    if (p === '/api/ai-review/csrf') return json(response, { token: '隔离测试令牌' })
    if (p === '/api/ai-review/config') return json(response, { config: { enabled: true }, health: ['worker', 'feedback', 'progress'].map(name => ({ name, updated: now })) })
    if (p === '/api/ai-review/repositories') return json(response, repositories)
    if (p === '/api/ai-review/servers' || p === '/api/servers') return json(response, servers)
    if (p === '/api/ai-review/attention') return json(response, { summary: { pr_count: 0 }, items: [], total: 0 })
    if (p === '/api/ai-review/tasks') return failPolling ? json(response, { error: '隔离验证：任务轮询断开' }, 503) : json(response, { items: [task], total: 1 })
    if (p === '/api/ai-review/statistics') return json(response, null)
    if (p === '/api/ai-review/tasks/test-task') return json(response, task)
    if (p === '/api/ai-review/tasks/test-task/issues') return json(response, { items: [], total: 0, checks: {} })
    if (p === '/api/ai-review/tasks/test-task/events') return json(response, { items: [], has_more: false })
    if (p.endsWith('/progress-assets')) return json(response, { error: '隔离验证：无权下载组件' }, 403)
    if (p === '/api/settings') return json(response, { host_ip: '127.0.0.1' })
    if (p === '/api/alerts/summary') return json(response, { active_count: 1, latest_alert: null })
    if (p === '/api/alerts') return json(response, [{ id: 1, status: 'active', message: '隔离验证告警', alert_type: 'backup_failed' }])
    if (p === '/api/schedules') return json(response, [{ id: 1, name: '隔离测试定时任务', source_server_id: 1, source_server_name: servers[0].name, target_ids: [], enabled: true, schedule_hour: 1, schedule_minute: 0 }])
    if (p === '/api/mirrors') return json(response, [{ id: 1, source_server_name: '测试源', target_server_name: '测试目标', status: 'success', total_repos: 1, synced_repos: 1 }])
    if (p === '/api/backups' || p === '/api/restore-tasks') return json(response, [])
    if (p.startsWith('/api/statistics/')) return json(response, { error: '隔离验证：统计查询失败' }, 500)
    return json(response, {})
  }
  const dist = path.join(root, 'frontend/dist'), relative = p === '/' ? 'index.html' : p.slice(1)
  const candidate = path.resolve(dist, relative)
  const file = candidate.startsWith(dist + path.sep) && fs.existsSync(candidate) && fs.statSync(candidate).isFile() ? candidate : path.join(dist, 'index.html')
  response.writeHead(200, { 'Content-Type': ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html' })[path.extname(file)] || 'application/octet-stream' })
  fs.createReadStream(file).pipe(response)
})

async function main() {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${server.address().port}`
  const browser = await chromium.launch({ headless: true })
  async function run(name, route, action) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
    try { await page.goto(base + route); await action(page); results.push({ 场景: name, 结果: '通过' }); console.log('通过：' + name) }
    catch (error) { results.push({ 场景: name, 结果: '失败', 原因: error.message }); throw error }
    finally { await page.close() }
  }
  async function popup(page, message) {
    const item = page.locator('.el-message--error', { hasText: message })
    await item.waitFor({ state: 'visible' })
    assert.equal(await item.count(), 1, '一次失败只能出现一条同类通知')
    assert.equal(await page.locator('.el-message--success').count(), 0, '失败操作不能显示成功通知')
    return item
  }
  try {
    await run('过期任务列表重审：错误弹出且成功轮询不会清除原因', '/ai-reviews', async page => {
      await page.getByRole('button', { name: '重新审查', exact: true }).click()
      await popup(page, 'PR 已关闭或合并')
      const reads = requests.filter(r => r.path === '/api/ai-review/tasks').length
      await page.waitForResponse(r => new URL(r.url()).pathname === '/api/ai-review/tasks' && r.status() === 200)
      assert.ok(requests.filter(r => r.path === '/api/ai-review/tasks').length > reads)
      assert.ok(await page.locator('.el-alert', { hasText: 'PR 已关闭或合并' }).isVisible())
      await page.screenshot({ path: path.join(output, 'review-error.png') })
    })
    await run('任务详情重审：当前详情展示原因', '/ai-reviews?task=test-task', async page => {
      await page.getByRole('region', { name: '审查任务详情' }).getByRole('button', { name: '重新审查', exact: true }).click()
      await popup(page, 'PR 已关闭或合并')
      assert.ok(await page.locator('.review-detail .el-alert', { hasText: 'PR 已关闭或合并' }).isVisible())
    })
    await run('全屏详情中的失败通知仍可见', '/ai-reviews?task=test-task', async page => {
      const detail = page.getByRole('region', { name: '审查任务详情' })
      await detail.getByRole('button', { name: '全屏', exact: true }).click()
      await page.waitForFunction(() => Boolean(document.fullscreenElement))
      await detail.getByRole('button', { name: '重新审查', exact: true }).click()
      await popup(page, 'PR 已关闭或合并')
      assert.ok(await page.evaluate(() => document.fullscreenElement.contains(document.querySelector('.el-message--error'))))
    })
    await run('人工审查表单失败保留表单与错误', '/ai-reviews', async page => {
      await page.getByRole('button', { name: '发起审查', exact: true }).click()
      const dialog = page.getByRole('dialog', { name: '人工触发 PR 审查' })
      await dialog.getByRole('button', { name: '开始审查', exact: true }).click()
      await popup(page, 'PR 已关闭或合并')
      assert.ok(await dialog.locator('.el-alert').isVisible())
    })
    await run('后续操作成功后清除上次操作错误', '/ai-reviews', async page => {
      const button = page.getByRole('button', { name: '重新审查', exact: true })
      await button.click(); const error = await popup(page, 'PR 已关闭或合并')
      await error.locator('.el-message__closeBtn').click()
      rerunAccepted = true
      try {
        await button.click()
        await page.locator('.el-message--success', { hasText: '审查任务已排队' }).waitFor()
        assert.equal(await page.locator('.el-alert', { hasText: 'PR 已关闭或合并' }).count(), 0)
      } finally { rerunAccepted = false }
    })
    await run('服务器保存失败保留编辑框', '/servers', async page => {
      await page.getByRole('button', { name: '编辑', exact: true }).click()
      const dialog = page.getByRole('dialog', { name: '编辑服务器' })
      await dialog.getByRole('button', { name: '保存', exact: true }).click()
      await popup(page, '隔离验证：操作被服务拒绝')
      assert.ok(await dialog.isVisible())
    })
    await run('删除影响检查失败后绝不发送删除请求', '/servers', async page => {
      const start = requests.length
      await page.getByRole('button', { name: '删除', exact: true }).click()
      await page.getByRole('button', { name: '确定', exact: true }).click()
      await popup(page, '删除影响检查失败，已停止删除')
      assert.equal(requests.slice(start).filter(r => r.method === 'DELETE').length, 0)
    })
    await run('镜像同步失败显示原因', '/mirrors', async page => {
      await page.getByRole('button', { name: '同步', exact: true }).click()
      await popup(page, '隔离验证：操作被服务拒绝')
    })
    await run('定时任务开关失败显示原因并回退开关', '/schedules', async page => {
      const toggle = page.getByRole('switch')
      await page.locator('.el-switch').waitFor(); assert.equal(await toggle.getAttribute('aria-checked'), 'true')
      await page.locator('.el-switch').click(); await popup(page, '隔离验证：操作被服务拒绝')
      assert.equal(await toggle.getAttribute('aria-checked'), 'true')
    })
    await run('统计页原本吞掉的错误现在可见', '/statistics/authors?server_id=1', async page => { await popup(page, '隔离验证：统计查询失败') })
    await run('系统设置保存失败显示原因', '/settings', async page => {
      await page.getByRole('button', { name: '保存', exact: true }).click()
      await popup(page, '隔离验证：操作被服务拒绝')
    })
    await run('组件下载 Blob 错误显示服务端原因', '/ai-reviews/repositories', async page => {
      await page.getByRole('button', { name: '下载 PR 进度组件', exact: true }).click()
      await popup(page, '隔离验证：无权下载组件')
    })
    await run('后台轮询失败仅显示区域错误，不连续弹窗', '/ai-reviews', async page => {
      await page.getByRole('button', { name: '重新审查', exact: true }).waitFor()
      failPolling = true
      await page.waitForResponse(r => new URL(r.url()).pathname === '/api/ai-review/tasks' && r.status() === 503)
      await page.locator('.el-alert', { hasText: '隔离验证：任务轮询断开' }).waitFor()
      assert.equal(await page.locator('.el-message--error').count(), 0)
      failPolling = false
    })
  } finally {
    fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify({ 范围: '生产前端构建与隔离 HTTP 故障注入，未验证用户部署环境', 场景: results }, null, 2) + '\n')
    await browser.close(); await new Promise(resolve => server.close(resolve))
  }
  console.log(`浏览器验收通过：${results.length} 个场景；记录：${output}`)
}
main().catch(error => { console.error('浏览器验收失败：' + error.message); process.exitCode = 1 })
