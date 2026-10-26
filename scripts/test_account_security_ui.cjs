// 真实 Flask/SQLite 与生产前端构建的隔离浏览器验收；目录账号展示单独使用明确的接口替身。
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const http = require('node:http')
const { spawn, execFileSync } = require('node:child_process')
const { randomBytes } = require('node:crypto')
const { chromium } = require('playwright')
const root = path.resolve(__dirname, '..')
const output = path.join(root, 'work/account-security')
fs.mkdirSync(output, { recursive: true })
const results = [], pageErrors = []
const append = results.push.bind(results)
results.push = (...items) => { console.log(items.join('\n')); return append(...items) }
let fixture, server, browser
const packaged = process.argv.includes('--packaged'), containers = []
let dockerNetwork
const initialPassword = 'admin123', firstPassword = '隔离验收-安全密码-123', nextPassword = '隔离验收-新的密码-456'

async function startFixture() {
  const python = process.env.ACCOUNT_SECURITY_PYTHON || path.join(root, 'work/access-native-venv/bin/python')
  fixture = spawn(python, [path.join(root, 'scripts/account_security_fixture.py')], {
    cwd: root, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' }, stdio: ['ignore', 'pipe', 'pipe'],
  })
  return new Promise((resolve, reject) => {
    let data = '', errors = ''
    const timer = setTimeout(() => reject(new Error('隔离后端未及时启动')), 30000)
    fixture.stderr.on('data', chunk => { errors += chunk.toString() })
    fixture.stdout.on('data', chunk => {
      data += chunk.toString()
      const line = data.split('\n').find(line => line.startsWith('验收服务：'))
      if (line) { clearTimeout(timer); resolve(JSON.parse(line.slice('验收服务：'.length))) }
    })
    fixture.on('exit', code => { clearTimeout(timer); reject(new Error('隔离后端退出：' + code + '\n' + errors.slice(-3000))) })
  })
}

async function startPackaged() {
  const tag = process.env.ACCOUNT_SECURITY_IMAGE_TAG
  assert.ok(tag && /^[a-zA-Z0-9_.-]+$/.test(tag), '交付镜像测试必须明确指定固定镜像标签')
  const docker = args => execFileSync('docker', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim()
  dockerNetwork = 'gm-account-security-' + randomBytes(5).toString('hex')
  const networkArgs = ['network', 'create', '--label', 'io.gitea-manager.account-security-test=true']
  try { docker([...networkArgs, dockerNetwork]) }
  catch (failure) {
    if (!String(failure.stderr).includes('address pools have been fully subnetted')) throw failure
    // 默认地址池已用完时使用测试专用的文档地址段，不删除用户已有网络；入口仅绑定回环地址。
    let created = false
    for (const subnet of ['192.0.2.0/28', '198.51.100.0/28', '203.0.113.0/28']) {
      try { docker([...networkArgs, '--subnet', subnet, dockerNetwork]); created = true; break } catch {}
    }
    assert.ok(created, '无法分配独立验收网络')
  }
  const backend = dockerNetwork + '-backend', frontend = dockerNetwork + '-frontend'
  docker(['run', '-d', '--rm', '--platform', 'linux/amd64', '--name', backend, '--network', dockerNetwork,
    '--network-alias', 'backend', '--label', 'io.gitea-manager.account-security-test=true', '--read-only',
    '--tmpfs', '/app/data:rw,mode=1777', '--tmpfs', '/tmp:rw,mode=1777',
    '-e', 'DATABASE_URL=sqlite:////app/data/manager.db', '-e', 'AI_REVIEW_DB=/app/data/review.db',
    '-e', 'SECRET_KEY=' + randomBytes(48).toString('hex'), '-e', 'INIT_PASSWORD=admin123',
    '-e', 'DISABLE_BACKGROUND_TASKS=1', 'gitea-review-backend:' + tag])
  containers.push(backend)
  docker(['run', '-d', '--rm', '--platform', 'linux/amd64', '--name', frontend, '--network', dockerNetwork,
    '--label', 'io.gitea-manager.account-security-test=true', '--read-only', '--tmpfs', '/tmp:rw,mode=1777',
    '--tmpfs', '/var/cache/nginx:rw,mode=1777', '--tmpfs', '/var/run:rw,mode=1777',
    '-p', '127.0.0.1::80', 'gitea-review-frontend:' + tag])
  containers.push(frontend)
  const address = docker(['port', frontend, '80/tcp']), base = 'http://' + address
  for (let attempt = 0; attempt < 60; attempt++) {
    try { if ((await fetch(base + '/api/session')).ok) return { base } } catch {}
    await new Promise(resolve => setTimeout(resolve, 500))
  }
  throw new Error('交付镜像的隔离服务未就绪')
}

async function serve(backendPort) {
  server = http.createServer((request, response) => {
    if (request.url.startsWith('/api/')) {
      const upstream = http.request({ hostname: '127.0.0.1', port: backendPort, path: request.url,
        method: request.method, headers: { ...request.headers } }, source => {
        response.writeHead(source.statusCode, source.headers); source.pipe(response)
      })
      upstream.on('error', () => { response.writeHead(502); response.end('隔离后端连接失败') })
      request.pipe(upstream); return
    }
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

async function fill(page, current, password, confirmation = password) {
  await page.getByLabel('当前密码', { exact: true }).fill(current)
  await page.getByLabel('新密码', { exact: true }).fill(password)
  await page.getByLabel('确认新密码', { exact: true }).fill(confirmation)
}

async function signIn(page, base, password) {
  await page.goto(base + '/account/security')
  await page.getByLabel('密码', { exact: true }).fill(password)
  await page.getByRole('button', { name: '登录', exact: true }).click()
}

async function geometry(page, width) {
  await page.setViewportSize({ width, height: 1024 })
  await page.waitForTimeout(300)
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), '页面不得横向溢出')
  const violations = await page.locator('.app-menu .el-icon, .password-visibility, .password-session-note > svg').evaluateAll(nodes => nodes.filter(node => {
    const box = node.getBoundingClientRect(), style = getComputedStyle(node)
    return box.width && box.height && (style.position === 'absolute' || box.left < 0 || box.right > innerWidth + 1)
  }).map(node => ({ 标签: node.outerHTML.slice(0, 100), 定位: getComputedStyle(node).position,
    左边: node.getBoundingClientRect().left, 右边: node.getBoundingClientRect().right })))
  await page.screenshot({ path: path.join(output, `account-${width}.png`), fullPage: true })
  assert.deepEqual(violations, [], '图标必须留在可视范围和正常文档流')
}

async function main() {
  const info = packaged ? await startPackaged() : await startFixture(), base = info.base || await serve(info['端口'])
  browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({ viewport: { width: 1536, height: 1024 } })
  const page = await context.newPage()
  page.on('pageerror', error => pageErrors.push(error.message))
  await signIn(page, base, initialPassword)
  await page.getByRole('heading', { name: '设置安全的管理员密码' }).waitFor()
  await fill(page, initialPassword, firstPassword)
  await page.getByRole('button', { name: '保存新密码', exact: true }).click()
  await page.getByRole('button', { name: '账号与安全', exact: true }).click()
  await page.getByRole('heading', { name: '当前账号' }).waitFor()
  assert.equal((await context.request.get(base + '/api/session')).ok(), true)
  assert.equal((await (await context.request.get(base + '/api/session')).json()).needs_config, true)
  results.push('默认管理员首次强制改密成功；未配置本机 IP 时可直接进入日常改密页')
  const session = await (await context.request.get(base + '/api/session')).json()
  await context.request.post(base + '/api/settings', { data: { host_ip: '127.0.0.1' }, headers: { 'X-CSRF-Token': session.csrf_token } })
  await page.reload()
  await page.getByRole('heading', { name: '当前账号' }).waitFor()

  for (const [password, confirm, message] of [['短密码', '短密码', '新密码至少需要 12 位'], [firstPassword, firstPassword, '新密码不能与当前密码相同'], [nextPassword, '不一致', '两次新密码不一致']]) {
    await fill(page, firstPassword, password, confirm)
    await page.getByRole('button', { name: '保存新密码', exact: true }).click()
    await page.getByText(message, { exact: true }).waitFor()
  }
  await fill(page, '错误的当前密码', nextPassword)
  await page.getByRole('button', { name: '保存新密码', exact: true }).click()
  await page.getByText('当前密码不正确', { exact: true }).waitFor()
  assert.equal((await (await context.request.get(base + '/api/session')).json()).authenticated, true)
  results.push('短密码、同密码、确认不一致和错误旧密码均明确拒绝，当前登录保持有效')

  const eye = page.getByRole('button', { name: '显示当前密码', exact: true })
  await eye.click()
  assert.equal(await page.getByLabel('当前密码', { exact: true }).getAttribute('type'), 'text')
  await page.getByRole('button', { name: '隐藏当前密码', exact: true }).click()
  await page.getByRole('button', { name: '清空', exact: true }).click()
  for (const label of ['当前密码', '新密码', '确认新密码']) assert.equal(await page.getByLabel(label, { exact: true }).inputValue(), '')
  results.push('显示/隐藏密码、清空表单与字段标签正常')

  for (const width of [1536, 1024, 768, 390]) await geometry(page, width)
  await page.setViewportSize({ width: 1536, height: 1024 })
  await page.getByRole('button', { name: '收起导航', exact: true }).click()
  await geometry(page, 1536)
  await page.screenshot({ path: path.join(output, 'account-collapsed.png') })
  await page.getByRole('button', { name: '展开导航', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('.app-aside')?.getBoundingClientRect().width === 240)
  await page.screenshot({ path: path.join(output, 'account-desktop.png') })
  await page.locator('.app-aside').screenshot({ path: path.join(output, 'navigation-icons.png') })
  await page.addScriptTag({ path: path.join(root, 'frontend/node_modules/axe-core/axe.min.js') })
  const accessibility = await page.evaluate(async () => (await axe.run('.account-security')).violations.map(row => ({ 规则: row.id, 影响: row.impact, 数量: row.nodes.length })))
  assert.deepEqual(accessibility, [], '账号页面不能存在无障碍规则违规')
  const symbols = await page.locator('.app-menu .el-icon:not(.el-sub-menu__icon-arrow) > svg').evaluateAll(nodes => nodes.map(node => ({ stroke: node.getAttribute('stroke-width'), fill: node.getAttribute('fill') })))
  assert.ok(symbols.length >= 18 && symbols.every(icon => icon.stroke === '2' && icon.fill === 'none'))
  results.push('1536、1024、768、390 宽度与菜单折叠正常，导航统一采用细线轮廓图标；账号页面无障碍检查通过')

  const second = await browser.newContext(), other = await second.newPage()
  await signIn(other, base, firstPassword)
  await other.getByRole('button', { name: '账号与安全', exact: true }).click()
  await other.getByRole('heading', { name: '当前账号' }).waitFor()
  await fill(page, firstPassword, nextPassword)
  const before = await (await context.request.get(base + '/api/session')).json()
  await page.getByRole('button', { name: '保存新密码', exact: true }).click()
  await page.getByText('密码已修改，其他登录会话已退出', { exact: true }).waitFor()
  const after = await (await context.request.get(base + '/api/session')).json()
  assert.equal(after.authenticated, true); assert.notEqual(after.csrf_token, before.csrf_token)
  assert.equal((await (await second.request.get(base + '/api/session')).json()).authenticated, false)
  await other.reload(); await other.getByLabel('密码', { exact: true }).waitFor()
  const saved = await context.request.post(base + '/api/settings', { data: { host_ip: '127.0.0.1' }, headers: { 'X-CSRF-Token': after.csrf_token } })
  assert.equal(saved.status(), 200)
  results.push('真实双浏览器会话：改密后当前会话和新 CSRF 可用，其他会话失效')
  await second.close()

  let neverSettles
  await page.route('**/api/access/password', route => { neverSettles = route })
  await fill(page, nextPassword, firstPassword)
  await page.getByRole('button', { name: '保存新密码', exact: true }).click()
  assert.equal(await page.getByLabel('当前密码', { exact: true }).isDisabled(), true)
  assert.equal(await page.getByRole('button', { name: '清空', exact: true }).isDisabled(), true)
  await page.getByRole('menuitem', { name: '系统设置', exact: true }).click()
  await page.getByRole('heading', { name: '系统设置', exact: true }).waitFor()
  await page.getByRole('button', { name: '账号与安全', exact: true }).click()
  await page.getByRole('heading', { name: '当前账号' }).waitFor()
  assert.equal(await page.getByLabel('当前密码', { exact: true }).inputValue(), '')
  await neverSettles.abort().catch(() => {})
  await page.unroute('**/api/access/password')
  results.push('保存中禁止重复操作；离开页面取消请求并清除密码')

  await page.route('**/api/access/password', route => route.abort('failed'))
  await fill(page, nextPassword, firstPassword)
  await page.getByRole('button', { name: '保存新密码', exact: true }).click()
  await page.getByText('密码修改未确认，请检查连接；如已生效，请使用新密码重新登录', { exact: true }).waitFor()
  await page.unroute('**/api/access/password')
  await page.getByRole('button', { name: '清空', exact: true }).click()
  results.push('网络中断显示结果未确认，不误报成功，仍可清空或重新登录')

  // 目录服务不在本次真实外部接入范围；此处只验证服务端已明确提供的身份对应界面。
  await page.route('**/api/session', route => route.fulfill({ json: { ...after, capabilities: [], organizations: [],
    user: { id: 99, username: 'admin', display_name: '目录测试账号', is_platform_admin: false, must_change_password: false, auth_source: 'ldap' } } }))
  await page.reload()
  await page.getByRole('heading', { name: '请在公司目录中修改密码', exact: true }).waitFor()
  assert.equal(await page.getByLabel('当前密码', { exact: true }).count(), 0)
  results.push('目录账号与本地账号同名、未分配权限时仍展示正确目录说明，不提供本地改密表单（展示替身）')
  assert.deepEqual(pageErrors, [])
}

main().then(() => console.log('通过：' + results.join('\n通过：'))).catch(async error => {
  const page = browser?.contexts()[0]?.pages()[0]
  if (page) { await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }); console.error('失败页面：', page.url(), await page.locator('body').innerText()) }
  results.push('失败：' + error.message); console.error(error); process.exitCode = 1
}).finally(async () => {
  fs.writeFileSync(path.join(output, packaged ? 'acceptance-packaged.json' : 'acceptance.json'), JSON.stringify({ 说明: packaged ? 'Linux amd64 最终镜像、原始 nginx 与双 Gunicorn worker、全新临时数据库；未挂载源码；目录说明仅作展示替身；未访问现有部署' : '全新隔离数据库与真实认证接口；目录说明仅作界面替身验证；未访问现有部署', 检查: results, 浏览器异常: pageErrors }, null, 2) + '\n')
  await browser?.close(); server?.close(); fixture?.kill('SIGTERM')
  for (const name of containers.reverse()) { try { execFileSync('docker', ['rm', '-f', name], { stdio: 'ignore' }) } catch {} }
  if (dockerNetwork) { try { execFileSync('docker', ['network', 'rm', dockerNetwork], { stdio: 'ignore' }) } catch {} }
})
