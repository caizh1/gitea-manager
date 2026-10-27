// 使用隔离接口样本验证真实前端的表格成本确认，不连接用户的 Gitea 或模型服务。
const assert = require('node:assert/strict')
const fs = require('node:fs')
const http = require('node:http')
const path = require('node:path')
const { chromium } = require('playwright')

const root = path.resolve(__dirname, '..')
const output = path.join(root, 'work/review-cost-table')
fs.mkdirSync(output, { recursive: true })
const capabilities = ['dashboard', 'servers', 'backups', 'restore', 'settings', 'schedules', 'mirrors', 'statistics', 'svn_migrations', 'repository_policies', 'approval_rules', 'ai_reviews', 'commit_gates', 'access', 'alerts']
const session = { authenticated: true, needs_config: false, csrf_token: '隔离测试令牌', organizations: [], capabilities,
  user: { id: 1, username: 'admin', display_name: '测试管理员', is_platform_admin: true, auth_source: 'local', must_change_password: false } }
const repositories = [{ id: 1, server_name: '测试实例', repo_name: '测试仓库', enabled: true },
  { id: 2, server_name: '测试实例', repo_name: '归档仓库', enabled: false, access_archived: true }]
function summary(hash, max) { return { plan_hash: hash, primary: 4, association: 2, history: 1, base_calls: 7, max_calls: max,
  authorized_calls: 0, input_tokens: 12345, output_tokens: 8192, basis: '保守估算', model: '测试模型', revision: 'utf8-v1',
  changed_lines: 321, budget_seconds: 3600, gap_count: 1, unsupported_count: 2 } }
function task(id, number, cost, repository = 1) { return { id, number, repository, status: 'awaiting_cost', stage: '等待确认分批调用上限',
  batch_protocol: 1, created: Date.now() / 1000, updated: Date.now() / 1000, progress: { label: '等待成本确认', head: 'a'.repeat(40) },
  timing: { execution_seconds: 0 }, cost_summary: cost } }
let rows = [task('first', 11, summary('摘要一', 13)), task('second', 10, summary('摘要二', 14)), task('stale', 12, summary('旧摘要', 15)),
  task('missing', 13, null), task('archived', 14, summary('归档摘要', 17), 2)]
const posts = [], errors = [], unknown = []
let staleOnce = true, csrfRequests = 0, browser, server
function detail(row) {
  const c = row.cost_summary
  return { ...row, batch: c ? { hash: c.plan_hash, authorized_calls: c.authorized_calls, budget_seconds: c.budget_seconds,
    units: [], attempts: [], value: { ...c, profile: { basis: c.basis, model: c.model, revision: c.revision },
      config: { model_max_output_tokens: c.output_tokens }, gaps: [], unsupported: [] } } : null }
}
async function fixture(route) {
  const req = route.request(), p = new URL(req.url()).pathname, method = req.method()
  let value = {}, status = 200
  if (p === '/api/session') value = session
  else if (p === '/api/alerts/summary') value = { active_count: 0 }
  else if (p === '/api/alerts') value = []
  else if (p === '/api/ai-review/attention') value = { summary: { pr_count: 0 }, items: [], total: 0 }
  else if (p === '/api/ai-review/config') value = { config: { enabled: true }, health: ['worker', 'progress', 'feedback'].map(name => ({ name, updated: Date.now() / 1000 })) }
  else if (p === '/api/ai-review/servers') value = [{ id: 1, name: '测试实例' }]
  else if (p === '/api/ai-review/repositories') value = repositories
  else if (p === '/api/ai-review/statistics') value = null
  else if (p === '/api/ai-review/csrf') { csrfRequests++; await new Promise(resolve => setTimeout(resolve, 100)); value = { token: 'review-test-csrf' } }
  else if (p === '/api/ai-review/tasks') value = { items: rows, total: rows.length }
  else if (/^\/api\/ai-review\/tasks\/[^/]+\/issues$/.test(p)) value = { items: [], total: 0 }
  else if (/^\/api\/ai-review\/tasks\/[^/]+\/events$/.test(p)) value = { items: [], has_more: false }
  else if (/^\/api\/ai-review\/tasks\/[^/]+\/confirm-plan$/.test(p)) {
    const id = p.split('/')[4], data = req.postDataJSON(), row = rows.find(item => item.id === id)
    posts.push({ id, data, csrf: req.headers()['x-review-csrf'] })
    assert.equal(req.headers()['x-review-csrf'], 'review-test-csrf')
    if (id === 'stale' && staleOnce) {
      staleOnce = false; row.cost_summary = summary('新摘要', 19)
      status = 400; value = { error: '计划不存在或摘要已经变化，请刷新' }
    } else {
      assert.equal(data.plan_hash, row.cost_summary.plan_hash)
      await new Promise(resolve => setTimeout(resolve, 250))
      row.status = 'queued'; row.stage = '等待继续审查'; row.progress.label = '已排队'
      row.cost_summary = null; value = detail(row)
    }
  } else if (/^\/api\/ai-review\/tasks\/[^/]+$/.test(p)) value = detail(rows.find(item => item.id === p.split('/')[4]))
  else unknown.push(method + ' ' + p)
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })
}
async function main() {
  const dist = path.join(root, 'frontend/dist')
  server = http.createServer((req, res) => {
    const candidate = path.resolve(dist, '.' + new URL(req.url, 'http://localhost').pathname)
    const file = candidate.startsWith(dist + path.sep) && fs.existsSync(candidate) && fs.statSync(candidate).isFile() ? candidate : path.join(dist, 'index.html')
    res.writeHead(200, { 'Content-Type': ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html' })[path.extname(file)] || 'application/octet-stream' })
    fs.createReadStream(file).pipe(res)
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  browser = await chromium.launch({ headless: true, executablePath: process.env.REVIEW_CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' })
  const context = await browser.newContext({ viewport: { width: 1536, height: 1024 } })
  await context.route('**/api/**', fixture)
  const page = await context.newPage(); page.setDefaultTimeout(8000); page.on('pageerror', error => errors.push(error.message))
  await page.goto(`http://127.0.0.1:${server.address().port}/ai-reviews`)
  const table = page.locator('.task-table-panel .el-table__body')
  const row = n => table.locator('tr').filter({ hasText: '#' + n })
  await row(11).getByTestId('cost-summary').waitFor()
  assert.match(await row(11).innerText(), /基础调用 7 次，最多 13 次/)
  assert.match(await row(11).innerText(), /初始输入 12345 Token/)
  assert.match(await row(11).innerText(), /模型 测试模型 · 保守估算/)
  assert.match(await row(11).innerText(), /覆盖缺口 1 项 · 未支持内容 2 项/)
  assert.match(await row(11).innerText(), /无法估算金额/)
  assert.equal(await row(13).getByText('成本摘要暂不可用，请刷新或查看详情。').count(), 1)
  assert.equal(await row(13).getByRole('button', { name: /确认.*成本计划/ }).count(), 0)
  assert.equal(await row(14).getByRole('button', { name: /确认.*成本计划/ }).isDisabled(), true)
  await page.screenshot({ path: path.join(output, 'desktop.png'), animations: 'disabled' })
  const first = row(11).getByRole('button', { name: /确认.*成本计划/ })
  const second = row(10).getByRole('button', { name: /确认.*成本计划/ })
  await Promise.all([first.click(), second.click()])
  await first.dispatchEvent('click')
  await row(11).getByText('已排队', { exact: true }).waitFor()
  await row(10).getByText('已排队', { exact: true }).waitFor()
  assert.equal(posts.filter(post => post.id === 'first').length, 1)
  assert.equal(posts.filter(post => post.id === 'second').length, 1)
  assert.deepEqual(posts.find(post => post.id === 'first').data, { plan_hash: '摘要一' })
  assert.equal(csrfRequests, 1)
  await row(12).getByRole('button', { name: /确认.*成本计划/ }).click()
  await page.getByText('计划不存在或摘要已经变化，请刷新').first().waitFor()
  await row(12).getByText('基础调用 7 次，最多 19 次').waitFor()
  assert.equal(posts.filter(post => post.id === 'stale').length, 1)
  await row(12).getByRole('button', { name: '详情' }).click()
  const original = page.locator('.batch-plan').getByRole('button', { name: '确认此计划（最多 19 次）' })
  await original.waitFor()
  await original.click()
  await page.locator('.batch-plan').getByText('基础调用 7 次，含候选反证最多 19 次；已授权 0 次。').waitFor({ state: 'hidden' })
  assert.equal(posts.filter(post => post.id === 'stale').length, 2)
  assert.equal(posts.at(-1).data.plan_hash, '新摘要')
  await page.getByRole('button', { name: '返回完整列表' }).click()
  await page.setViewportSize({ width: 390, height: 844 })
  await row(13).waitFor()
  await page.screenshot({ path: path.join(output, 'mobile.png'), animations: 'disabled' })
  assert.deepEqual(errors, [])
  assert.deepEqual(unknown, [])
  console.log('表格摘要、逐项确认、重复点击、过期摘要、缺失摘要、归档限制、详情入口和窄屏渲染通过。')
}
main().catch(error => { console.error(error); process.exitCode = 1 }).finally(async () => { await browser?.close(); server?.close() })
