// 使用真实路由与页面、固定接口替身验证导航生命周期，不访问用户服务或业务数据。
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const http = require('node:http')
const { pathToFileURL } = require('node:url')
const { chromium } = require('playwright')

const root = path.resolve(__dirname, '..')
const output = path.join(root, 'work/route-navigation')
const sourceMode = process.argv.includes('--source')
const mode = sourceMode ? '源码' : process.env.NAVIGATION_BASE_URL ? '交付镜像' : '生产构建'
const capabilities = ['dashboard', 'servers', 'backups', 'restore', 'settings', 'schedules', 'mirrors',
  'statistics', 'svn_migrations', 'git_migrations', 'repository_policies', 'approval_rules', 'ai_reviews',
  'commit_gates', 'access', 'management', 'alerts']
const admin = { authenticated: true, needs_config: false, csrf_token: '隔离测试令牌', organizations: [],
  capabilities, user: { id: 1, username: '隔离管理员', display_name: '隔离管理员', is_platform_admin: true,
    auth_source: 'local', must_change_password: false } }
const servers = [{ id: 1, name: '隔离测试服务器', host: '127.0.0.1', role: 'primary', status: 'online',
  gitea_port: 3000, is_local: true, version: '1.26.4', repo_count: 0, user_count: 1 }]
let browser, server, vite, session = admin, delayServers = 0, failServers = false
const results = [], unknown = new Set(), errors = [], transitionWarnings = []
let serverReads = 0

function fixture(url, method) {
  const p = new URL(url).pathname
  if (p === '/api/session') return session
  if (p === '/api/servers') { serverReads++; return failServers ? { error: '隔离测试：服务器列表暂时不可用' } : servers }
  if (p === '/api/servers/1/detail') return { ...servers[0], detail: { backups: [], restores: [] } }
  if (p === '/api/settings') return { host_ip: '127.0.0.1' }
  if (p === '/api/dashboard/recent') return { recent_backups: [], recent_restores: [] }
  if (p === '/api/alerts/summary') return { active_count: 0, latest_alert: null }
  if (p === '/api/ai-review/attention') return { summary: { pr_count: 0 }, items: [], total: 0 }
  if (p.endsWith('/csrf')) return { token: '隔离测试令牌' }
  if (p === '/api/ai-review/config') return { config: { enabled: false }, health: [] }
  if (p === '/api/ai-review/statistics') return null
  if (p === '/api/ai-review/tasks') return { items: [], total: 0 }
  if (p === '/api/repository-policies/schema') return { fields: [] }
  if (p === '/api/access/directory') return { enabled: false }
  if (p === '/api/approval-rules/policies' || p === '/api/approval-rules/native-governance') return { items: [] }
  if (p === '/api/management/status') return { runtime: { ready: false, reason: '隔离导航验收不启动执行器' }, csrf: '隔离测试令牌' }
  if (p === '/api/management/ssh-public-key') return { public_key: '隔离测试公钥占位' }
  if (p === '/api/management/diagnostics') return { reports: [] }
  if (p === '/api/management/discover' && method === 'POST') return { instances: [], diagnostic_id: null }
  if (/^\/api\/(svn|git)-migrations\/status$/.test(p)) return { worker: { online: false } }
  if (/^\/api\/svn-migrations\/(runs|preflights|plans)\/\d+$/.test(p)) return { error: '隔离测试：详情记录不存在' }
  if (p === '/api/statistics/1/overview') return { total_commits: 0, total_code_lines: 0, total_doc_lines: 0,
    total_other_lines: 0, total_repos: 0, top_languages: [] }
  if (/^\/api\/statistics\/1\/authors\/[^/]+$/.test(p)) return { total_commits: 0, total_additions: 0, total_deletions: 0, repo_count: 0 }
  if (p.startsWith('/api/statistics/1/')) return []
  if (['/api/backups', '/api/restore-tasks', '/api/schedules', '/api/mirrors', '/api/commit-rules',
    '/api/commit-gates/repos', '/api/alerts', '/api/ai-review/servers', '/api/ai-review/repositories',
    '/api/repository-policies/servers', '/api/access/accounts', '/api/access/organizations'].includes(p)) return []
  if (/^\/api\/(svn|git)-migrations\/(sources|plans|runs|targets)$/.test(p)) return []
  unknown.add(method + ' ' + p)
  return { error: '隔离测试未定义的接口' }
}

async function startServer() {
  if (process.env.NAVIGATION_BASE_URL) return process.env.NAVIGATION_BASE_URL
  if (sourceMode) {
    const { createServer } = await import(pathToFileURL(path.join(root, 'frontend/node_modules/vite/dist/node/index.js')))
    vite = await createServer({ root: path.join(root, 'frontend'), server: { middlewareMode: true }, appType: 'spa' })
  }
  server = http.createServer((request, response) => {
    if (vite) return vite.middlewares(request, response)
    const dist = path.join(root, 'frontend/dist')
    const candidate = path.resolve(dist, '.' + new URL(request.url, 'http://localhost').pathname)
    const file = candidate.startsWith(dist + path.sep) && fs.existsSync(candidate) && fs.statSync(candidate).isFile()
      ? candidate : path.join(dist, 'index.html')
    response.writeHead(200, { 'Content-Type': ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html' })[path.extname(file)] || 'application/octet-stream' })
    fs.createReadStream(file).pipe(response)
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  return `http://127.0.0.1:${server.address().port}`
}

async function navigate(page, target) {
  await page.evaluate(async target => {
    await document.querySelector('#app').__vue_app__.config.globalProperties.$router.push(target)
  }, target)
}

async function ready(page, expected) {
  await page.waitForFunction(expected => {
    const root = document.querySelector('.app-main'), child = root?.firstElementChild
    return child && getComputedStyle(child).opacity !== '0' && root.innerText.trim()
      && !root.querySelector('.page-enter-active, .page-leave-active')
      && (!expected || root.innerText.includes(expected))
  }, expected, { timeout: 5000 })
}

async function serverReady(page) {
  await ready(page, '服务器管理')
  await page.locator('.el-table__body').getByText('隔离测试服务器', { exact: true }).waitFor({ timeout: 5000 })
}

async function main() {
  fs.mkdirSync(output, { recursive: true })
  const base = await startServer()
  browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  await context.route(/^https?:\/\/[^/]+\/api\//, async route => {
    const p = new URL(route.request().url()).pathname
    const data = fixture(route.request().url(), route.request().method())
    if (p === '/api/servers' && delayServers) await new Promise(resolve => setTimeout(resolve, delayServers))
    const failure = (p === '/api/servers' && failServers) || /^\/api\/svn-migrations\/(runs|preflights|plans)\/\d+$/.test(p)
    await route.fulfill({ status: failure ? 503 : 200, contentType: 'application/json', body: JSON.stringify(data) }).catch(() => {})
  })
  const page = await context.newPage()
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => {
    if (message.type() === 'warning' && /Transition.*non-element|non-element root.*animated/i.test(message.text())) transitionWarnings.push(message.text())
  })
  await page.goto(base + '/servers')
  await serverReady(page)

  const paths = await page.evaluate(() => document.querySelector('#app').__vue_app__.config.globalProperties.$router.getRoutes()
    .filter(route => !route.redirect).map(route => route.path.replace(/:name\b/g, '测试作者').replace(/:\w+/g, '1')))
  for (const target of paths) {
    await navigate(page, target + (target.startsWith('/statistics') ? '?server_id=1' : ''))
    await ready(page)
    // 留出正常的异步回填时间，确保不是只检查尚未完成加载的骨架。
    await page.waitForTimeout(150)
    await navigate(page, '/servers')
    await serverReady(page)
    results.push({ 场景: '页面往返', 地址: target, 结果: '通过' })
  }
  console.log(`${mode}：${paths.length} 个路由入口往返通过`)

  for (const reducedMotion of ['no-preference', 'reduce']) {
    await page.emulateMedia({ reducedMotion })
    for (const gap of [0, 25, 120, 300]) {
      for (let round = 0; round < 3; round++) {
        for (const name of ['备份管理', '恢复操作', '系统设置', '定时任务', '服务器管理']) {
          await page.getByRole('menuitem', { name, exact: true }).evaluate(node => node.click())
          if (gap) await page.waitForTimeout(gap)
        }
        await serverReady(page)
      }
      results.push({ 场景: '连续菜单切换', 动画: reducedMotion, 点击间隔毫秒: gap, 轮次: 3, 结果: '通过' })
    }
  }

  delayServers = 250
  for (let round = 0; round < 3; round++) {
    await navigate(page, '/settings'); await ready(page, '系统设置')
    await navigate(page, '/servers'); await page.waitForTimeout(30)
    await navigate(page, '/backups'); await ready(page, '备份管理')
    await navigate(page, '/servers'); await serverReady(page)
  }
  delayServers = 0
  results.push({ 场景: '列表请求未完成时离开并返回', 轮次: 3, 结果: '通过' })

  await navigate(page, '/settings'); await ready(page, '系统设置')
  failServers = true
  await navigate(page, '/servers'); await ready(page, '服务器管理')
  await page.getByText('隔离测试：服务器列表暂时不可用', { exact: true }).waitFor()
  failServers = false
  await navigate(page, '/settings'); await ready(page, '系统设置')
  await navigate(page, '/servers'); await serverReady(page)
  // 当前列表缺少本地 catch，失败请求的未处理拒绝是已有行为，单独记录，不能伪装成导航故障。
  const requestFailures = errors.splice(0).filter(error => !/Request failed with status code 503/.test(error))
  assert.deepEqual(requestFailures, [], '不能出现页面渲染异常')
  results.push({ 场景: '列表接口失败后重新进入可恢复', 结果: '通过', 已有非阻塞行为: '请求失败会产生未处理的 Promise 拒绝，界面错误提示及后续导航正常' })

  session = { ...admin, user: { ...admin.user, id: 2, is_platform_admin: false }, organizations: [
    { binding_id: 1, server_id: 1, organization_id: 1, organization_name: '隔离测试组织', server_name: servers[0].name }],
    capabilities: ['dashboard', 'servers', 'settings'] }
  await page.reload(); await serverReady(page)
  await navigate(page, '/dashboard'); await ready(page, '隔离测试组织')
  await navigate(page, '/settings'); await ready(page, '系统设置')
  await navigate(page, '/servers'); await serverReady(page)
  results.push({ 场景: '组织管理员仪表盘根组件分支', 结果: '通过' })
  await page.screenshot({ path: path.join(output, sourceMode ? 'source-final.png' : 'build-final.png') })

  assert.deepEqual(errors, [], '不能出现新增页面渲染异常')
  assert.deepEqual(transitionWarnings, [], '不能出现非元素根节点动画告警')
  assert.deepEqual([...unknown], [], '接口替身必须覆盖所有实际请求')
  assert.ok(serverReads >= paths.length, '返回服务器页面后必须重新查询列表')
  console.log(`${mode}：全部导航验证通过，服务器列表查询 ${serverReads} 次`)
}

main().catch(error => { results.push({ 场景: '验收失败', 原因: error.message }); console.error(error); process.exitCode = 1 }).finally(async () => {
  fs.mkdirSync(output, { recursive: true })
  fs.writeFileSync(path.join(output, sourceMode ? 'source.json' : process.env.NAVIGATION_BASE_URL ? 'image.json' : 'build.json'), JSON.stringify({
    范围: `${mode}、真实页面与路由、隔离接口、后台浏览器；不代表真实业务或生产环境验收`, 场景: results,
    浏览器异常: errors, 动画告警: transitionWarnings, 未覆盖接口: [...unknown], 服务器查询次数: serverReads,
  }, null, 2) + '\n')
  await browser?.close(); await vite?.close()
  if (server) await new Promise(resolve => server.close(resolve))
})
