// 使用真实生产前端和固定接口样本核对仓库管理布局、抽屉及移除交互，不访问用户服务。
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const http = require('node:http')
const { chromium } = require('playwright')
const root = path.resolve(__dirname, '..'), output = path.join(root, 'work/review-repositories')
fs.mkdirSync(output, { recursive: true })
const capabilities = ['dashboard', 'servers', 'backups', 'restore', 'settings', 'schedules', 'mirrors', 'statistics', 'svn_migrations', 'repository_policies', 'approval_rules', 'ai_reviews', 'commit_gates', 'access', 'alerts']
let session = { authenticated: true, needs_config: false, csrf_token: '隔离测试令牌', organizations: [], capabilities,
  user: { id: 1, username: 'admin', display_name: '平台管理员', is_platform_admin: true, auth_source: 'local', must_change_password: false } }
const names = ['core/service-api', 'web/admin-console', 'platform/agent', 'tools/build-runner', 'archive/legacy-api', 'ops/deploy-check']
const servers = [{ id: 1, name: '研发主站' }, { id: 2, name: '工具链' }]
let repositories = names.map((name, index) => ({ id: index + 1, revision: 1, server_id: index < 3 ? 1 : 2,
  server_name: index < 3 ? '研发主站' : '工具链', repo_name: name, private: true, enabled: index < 4,
  bot_name: 'review-bot', hook_id: index < 5 ? 100 + index : null,
  hook_status: index < 5 ? '已安装并回读验证' : '未安装', last_event: index < 5 ? Date.now() / 1000 - [300, 720, 3600, 7200, 86400][index] : null }))
const config = { enabled: true, model: 'deepseek-v4-flash', api_base_url: 'https://model.invalid/v1', api_key_configured: true, revision: 1 }
let deleteFailure = false, setupRequests = 0, deleteRequests = 0, browser, server
const errors = [], unknown = [], results = []
function fixture(url, method, data) {
  const p = new URL(url).pathname
  if (p === '/api/session') return session
  if (p === '/api/alerts/summary') return { active_count: 0 }
  if (p === '/api/alerts') return []
  if (p === '/api/ai-review/attention') return { summary: { pr_count: 0 }, items: [], total: 0 }
  if (p === '/api/ai-review/config') return { config, health: ['worker', 'progress', 'feedback'].map(name => ({ name, updated: Date.now() / 1000 })) }
  if (p === '/api/ai-review/servers') return servers
  if (p === '/api/ai-review/repositories') return repositories
  if (p === '/api/ai-review/catalog') return { items: [{ id: 77, name: 'core/new-service' }] }
  if (p === '/api/ai-review/csrf') return { token: '隔离审查令牌' }
  if (p === '/api/ai-review/setup' && method === 'GET') return { automatic_available: true, job: null }
  if (p === '/api/ai-review/setup' && method === 'POST') { setupRequests++; assert.equal(data.repositories[0].repo_id, 77); return { job: { id: 'fixed', status: 'succeeded', server_id: 1, server_name: '研发主站', model: config.model, detail: { stage: '接入完成', results: [] } } } }
  if (p === '/api/ai-review/setup/fixed') return { job: { id: 'fixed', status: 'succeeded', server_id: 1, detail: {} } }
  const id = Number(p.split('/')[4]), row = repositories.find(row => row.id === id)
  if (row && method === 'DELETE') {
    deleteRequests++; assert.equal(data.revision, row.revision)
    row.enabled = false; row.access_archived = true; row.revision++
    if (deleteFailure) { row.removal_pending = true; row.removal_error = '隔离测试：通知清理连接超时'; return { error: '已停止新审查，通知清理连接超时' } }
    row.removal_pending = false; row.removal_complete = true; return row
  }
  if (row && method === 'PUT') { row.enabled = data.enabled; row.revision++; return row }
  if (p === '/api/ai-review/statistics') return null
  if (p === '/api/ai-review/tasks') return { items: [], total: 0 }
  unknown.push(method + ' ' + p); return {}
}
async function main() {
  server = http.createServer((req, res) => {
    const dist = path.join(root, 'frontend/dist'), candidate = path.resolve(dist, '.' + new URL(req.url, 'http://localhost').pathname)
    const file = candidate.startsWith(dist + path.sep) && fs.existsSync(candidate) && fs.statSync(candidate).isFile() ? candidate : path.join(dist, 'index.html')
    res.writeHead(200, { 'Content-Type': ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html' })[path.extname(file)] || 'application/octet-stream' }); fs.createReadStream(file).pipe(res)
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({ viewport: { width: 1536, height: 1024 } })
  await context.route('**/api/**', async route => {
    const request = route.request(), value = fixture(request.url(), request.method(), request.postDataJSON())
    await route.fulfill({ status: value.error ? 400 : 200, contentType: 'application/json', body: JSON.stringify(value) })
  })
  const page = await context.newPage(); page.setDefaultTimeout(8000); page.on('pageerror', error => errors.push(error.message))
  await page.goto((process.env.REVIEW_UI_BASE_URL || `http://127.0.0.1:${server.address().port}`) + '/ai-reviews/repositories')
  await page.locator('.repository-table').getByText('core/service-api', { exact: true }).waitFor()
  await page.locator('.el-loading-mask').waitFor({ state: 'hidden' })
  await page.getByRole('button', { name: 'core/service-api 的更多操作' }).click()
  await page.getByRole('menuitem', { name: '移除接入' }).waitFor()
  await page.mouse.move(260, 120)
  await page.screenshot({ path: path.join(output, 'desktop.png'), animations: 'disabled' })
  await page.keyboard.press('Escape')
  assert.equal(await page.locator('.repository-table .el-table__body tbody tr').count(), 6)
  assert.equal(await page.locator('.setup-panel').count(), 0)
  results.push('桌面首屏显示完整仓库列表，主页面不堆叠接入表单')
  console.log(results.at(-1))
  await page.getByRole('textbox', { name: '搜索仓库名称' }).fill('service-api')
  assert.equal(await page.locator('.repository-table .el-table__body tbody tr').count(), 1)
  await page.getByRole('textbox', { name: '搜索仓库名称' }).fill('')
  await page.locator('.repository-filters .el-select__wrapper').nth(1).click()
  await page.getByRole('option', { name: '已关闭', exact: true }).click()
  assert.equal(await page.locator('.repository-table .el-table__body tbody tr').count(), 2)
  await page.getByRole('combobox', { name: '筛选审查状态' }).focus(); await page.keyboard.press('Backspace')
  await page.locator('.repository-filters .el-select').nth(1).hover()
  await page.locator('.repository-filters .el-select').nth(1).locator('.el-select__clear').click()
  results.push('搜索与审查状态筛选可用')
  console.log(results.at(-1))
  await page.getByRole('button', { name: '接入仓库', exact: true }).click()
  const drawer = page.getByRole('dialog', { name: '接入审查仓库' })
  await drawer.getByText('快速接入 AI 审查').waitFor()
  await drawer.locator('.el-select__wrapper').nth(1).click()
  await page.getByRole('option', { name: /core\/new-service/ }).click()
  await page.keyboard.press('Escape')
  await drawer.getByRole('button', { name: '检查并启用' }).click()
  await page.waitForFunction(() => document.body.innerText.includes('接入完成'))
  assert.equal(setupRequests, 1)
  await page.screenshot({ path: path.join(output, 'setup-drawer.png'), animations: 'disabled' })
  await drawer.locator('.el-drawer__close-btn').click()
  results.push('接入抽屉的实例选择、仓库选择及提交可用')
  await page.getByRole('button', { name: 'core/service-api 的更多操作' }).click()
  await page.getByRole('menuitem', { name: '移除接入' }).click()
  let dialog = page.getByRole('dialog', { name: '移除审查接入' })
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  assert.equal(deleteRequests, 0)
  await page.getByRole('button', { name: 'core/service-api 的更多操作' }).click()
  await page.getByRole('menuitem', { name: '移除接入' }).click()
  dialog = page.getByRole('dialog', { name: '移除审查接入' }); deleteFailure = true
  await dialog.getByRole('button', { name: '移除接入', exact: true }).click()
  await dialog.getByText(/通知清理连接超时/).waitFor()
  await dialog.getByRole('button', { name: '取消', exact: true }).click()
  await page.locator('.repository-table').getByRole('button', { name: '重试移除', exact: true }).click()
  deleteFailure = false
  await page.getByRole('dialog', { name: '移除审查接入' }).getByRole('button', { name: '重试移除', exact: true }).click()
  await page.getByText('接入已移除，历史审查与 Gitea 仓库已保留').waitFor()
  await page.locator('.el-overlay:visible').waitFor({ state: 'hidden' })
  assert.equal(await page.locator('.repository-table').getByText('core/service-api', { exact: true }).count(), 0)
  results.push('取消不发请求；清理失败保留重试；成功移除后退出当前列表')
  for (const width of [768, 390]) {
    await page.setViewportSize({ width, height: 1000 })
    await page.screenshot({ path: path.join(output, `width-${width}.png`), fullPage: true, animations: 'disabled' })
    if (width === 390) {
      assert.ok(await page.locator('.repository-mobile-list').isVisible())
      for (const button of await page.locator('.repository-mobile-list button').all()) { const box = await button.boundingBox(); assert.ok(box && box.x >= 0 && box.x + box.width <= width + 1) }
    }
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), '页面整体不得水平溢出')
    const buttons = await page.locator('.heading-actions button').evaluateAll(nodes => nodes.map(node => { const r = node.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom } }))
    for (const b of buttons) assert.ok(b.left >= 0 && b.right <= width + 1, '主要按钮必须在屏幕内')
    results.push(`${width} 像素窄屏无整页溢出，主要按钮在可视范围内`)
  }
  session = { ...session, user: { ...session.user, is_platform_admin: false }, organizations: [{ binding_id: 1, server_id: 1, organization_id: 1, organization_name: '研发', server_name: '研发主站' }] }
  await page.setViewportSize({ width: 1536, height: 1024 }); await page.reload()
  await page.locator('.repository-table').getByText('web/admin-console', { exact: true }).waitFor()
  assert.equal(await page.getByRole('button', { name: '接入仓库', exact: true }).count(), 0)
  await page.getByRole('button', { name: 'web/admin-console 的更多操作' }).click()
  assert.equal(await page.getByRole('menuitem', { name: '移除接入' }).count(), 0)
  results.push('组织管理员无接入、事件通知写入或移除入口')
  assert.deepEqual(errors, []); assert.deepEqual(unknown, [])
  fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify({ 说明: '固定接口样本的生产前端交互验证，不代表实际 Gitea 集成验收', 结果: results }, null, 2) + '\n')
  console.log(results.join('\n'))
}
main().catch(error => { console.error(error); process.exitCode = 1 }).finally(async () => { if (browser) await browser.close(); if (server) await new Promise(resolve => server.close(resolve)) })
