// 专属实例上的真实浏览器验证；不替换接口响应，不创建备份恢复，不输出凭据。
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const crypto = require('node:crypto')
const { chromium } = require('playwright')

const root = fs.realpathSync(process.argv[2])
const state = JSON.parse(fs.readFileSync(path.join(root, 'private.json')))
const base = `http://127.0.0.1:${state.http_port}`
assert(Number.isInteger(state.http_port) && state.http_port > 1024 && state.http_port < 65536)
const stamp = Date.now().toString() + '-' + process.pid
const folder = path.join(root, 'browser', stamp)
fs.mkdirSync(folder, { recursive: true, mode: 0o700 })
const paths = ['/servers', '/backups', '/restore-tasks', '/schedules', '/settings', '/commit-rules', '/mirrors', '/alerts', '/alerts/summary']
const checks = [], violations = [], pageErrors = [], responses = []
const secrets = [state.manager_password, state.manager_secret]
for (const node of Object.values(state.nodes)) {
  for (const key of ['password', 'db_password', 'secret', 'token', 'original_token', 'scoped_token', 'totp', 'action_secret']) secrets.push(node[key])
  secrets.push(...Object.values(node.passwords || {}), ...Object.values(node.tokens || {}))
}
function scrub(value) {
  let result = String(value)
  for (const secret of secrets.filter(value => typeof value === 'string' && value.length)) result = result.split(secret).join('[已脱敏]')
  return result
}
function save(name, value) { fs.writeFileSync(path.join(folder, name), JSON.stringify(value, null, 2) + '\n', { mode: 0o600 }) }
function note(description) { checks.push({ 说明: description, 通过: true }) }
async function read(context, endpoint) {
  const response = await context.request.get(base + '/api' + endpoint)
  assert.equal(response.status(), 200, '实际管理接口读取失败：' + endpoint)
  return response.json()
}
async function snapshot(context) {
  const value = {}
  for (const endpoint of paths) value[endpoint] = await read(context, endpoint)
  for (const endpoint of ['/backups', '/restore-tasks']) assert(!value[endpoint].some(row => ['running', 'pending'].includes(row.status)), '已有活动任务，拒绝静态页面检查')
  return value
}

async function main() {
  let browser, context, page, offline = false, protectedReads = false
  const screenshot = name => page.screenshot({ path: path.join(folder, name), animations: 'disabled' })
  const result = { 说明: '冻结前端和真实后端的浏览器点击，仅覆盖空闲页面、历史失败与客户端断线；运行中任务、组织角色和完整管理类别仍未验证',
    状态: '执行中', 开始时间UTC: new Date().toISOString(), 工具SHA256: crypto.createHash('sha256').update(fs.readFileSync(__filename)).digest('hex'),
    Node版本: process.version, Playwright版本: require('playwright/package.json').version, 断言: checks }
  try {
    browser = await chromium.launch({ headless: true })
    result.浏览器版本 = browser.version()
    context = await browser.newContext({ viewport: { width: 1920, height: 1080 }, serviceWorkers: 'block' })
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      const foreign = url.origin !== base
      const unexpectedWrite = !['GET', 'HEAD', 'OPTIONS'].includes(request.method()) && !['/api/login', '/api/logout'].includes(url.pathname)
      if (foreign || unexpectedWrite) {
        violations.push({ 说明: foreign ? '拒绝访问测试入口以外地址' : '拒绝非登录写入', 方法: request.method(), 路径: url.pathname })
        await route.abort('blockedbyclient')
      } else await route.continue()
    })
    page = await context.newPage()
    page.setDefaultTimeout(12000)
    page.on('pageerror', error => pageErrors.push(scrub(error.message)))
    page.on('response', response => {
      const url = new URL(response.url())
      if (url.pathname.startsWith('/api/')) responses.push({ 路径: url.pathname, 状态: response.status(), 客户端断线中: offline, 验证会话期间: protectedReads })
    })
    await page.goto(base + '/backups')
    await page.locator('#login-password').waitFor({ state: 'visible' })
    assert.equal(await page.locator('.el-table__row').count(), 0, '未登录页面出现业务记录')
    note('浏览器未登录访问备份地址只显示真实登录表单')
    async function login() {
      await page.locator('#login-username').fill('admin')
      await page.locator('#login-password').fill(state.manager_password)
      await page.getByRole('button', { name: '登录', exact: true }).click()
      await page.locator('.app-main').waitFor({ state: 'visible' })
      assert.equal((await read(context, '/session')).authenticated, true)
    }
    await login()
    const before = await snapshot(context)
    protectedReads = true
    save('manager-before.json', before)
    const task = before['/backups'].find(row => row.status === 'failed' && row.error_msg && row.diagnostic_id)
    assert(task, '没有真实失败记录，不能用空页面验收错误显示')
    await page.getByRole('menuitem', { name: '备份管理', exact: true }).click()
    await page.locator('h3.section-title', { hasText: '备份管理' }).waitFor()
    const row = page.locator('.el-table__body-wrapper .el-table__row').filter({
      has: page.locator('td:first-child .cell').filter({ hasText: new RegExp('^' + task.id + '$') }),
    }).first()
    await row.waitFor({ state: 'visible' })
    await row.getByRole('button', { name: '展开详情', exact: true }).click()
    const stepsDialog = page.getByRole('dialog', { name: '执行步骤', exact: true })
    await stepsDialog.waitFor({ state: 'visible' })
    const steps = await read(context, `/backups/${task.id}/steps`)
    await page.waitForFunction(count => document.querySelectorAll('[role="dialog"] .operation-step').length === count, steps.length)
    assert(steps.length > 0 && steps.some(row => row.status === 'failed'))
    for (const step of steps) {
      const item = stepsDialog.locator('.operation-step').filter({ has: page.locator('strong', { hasText: step.label }) })
      assert.equal(await item.count(), 1, '真实步骤标签没有唯一显示')
      assert.equal(await item.locator('.el-tag').textContent(), step.status === 'failed' ? '失败' : step.status === 'success' ? '已完成' : '执行中')
    }
    await screenshot('backup-steps.png')
    await page.keyboard.press('Escape')
    await stepsDialog.waitFor({ state: 'hidden' })
    note('真实失败备份的全部历史步骤和失败状态可打开')
    await row.getByRole('button', { name: '查看', exact: true }).click()
    const errorDialog = page.getByRole('dialog', { name: '备份失败原因', exact: true })
    await errorDialog.waitFor({ state: 'visible' })
    assert.equal(await errorDialog.locator('.error-content').textContent(), task.error_msg, '页面错误正文与真实 API 不一致')
    await screenshot('backup-error.png')
    await page.keyboard.press('Escape')
    await errorDialog.waitFor({ state: 'hidden' })
    note('备份失败弹窗完整显示同一真实任务的错误原因')
    const active = before['/alerts/summary'].active_count
    await page.getByRole('button', { name: `告警中心，${active} 条未解决`, exact: true }).click()
    const popover = page.locator('.alert-popover:visible')
    await popover.waitFor({ state: 'visible' })
    await page.waitForFunction(count => document.querySelectorAll('.alert-popover .alert-item').length === count, before['/alerts'].length)
    for (const alert of before['/alerts']) assert((await popover.textContent()).includes(alert.message.slice(0, 120)), '真实告警未显示')
    await screenshot('alerts.png')
    await page.getByRole('button', { name: `告警中心，${active} 条未解决`, exact: true }).click()
    note('真实告警列表和未解决计数与 API 一致，未清除告警')
    await page.reload()
    await row.waitFor({ state: 'visible' })
    note('整页刷新后仍能读取相同的历史失败备份')
    const warning = page.getByText('进度刷新失败，保留最后记录；正在等待连接恢复。', { exact: true })
    try {
      offline = true
      await context.setOffline(true)
      await page.getByRole('button', { name: '刷新', exact: true }).click()
      await warning.waitFor({ state: 'visible' })
      await row.waitFor({ state: 'visible' })
      await screenshot('client-offline.png')
      note('仅浏览器客户端断线时明确提示刷新失败并保留历史记录')
    } finally {
      await context.setOffline(false)
      offline = false
    }
    await page.getByRole('button', { name: '刷新', exact: true }).click()
    await warning.waitFor({ state: 'hidden' })
    assert.equal((await read(context, '/session')).authenticated, true)
    await row.waitFor({ state: 'visible' })
    note('客户端恢复连接后刷新成功、刷新失败提示消失且登录保持')
    for (const [route, title] of [['servers', '服务器管理'], ['restore', '恢复操作'], ['schedules', '定时任务'], ['mirrors', '镜像管理'], ['commit-gates', '提交门禁'], ['settings', '系统设置']]) {
      await page.getByRole('menuitem', { name: title, exact: true }).click()
      await page.locator('h3.section-title', { hasText: title }).waitFor({ state: 'visible' })
      assert.equal(new URL(page.url()).pathname, '/' + route)
      await screenshot(route + '.png')
      note('真实导航打开页面：' + title)
    }
    const after = await snapshot(context)
    save('manager-after.json', after)
    assert.deepEqual(after, before, '页面操作改变了九类管理业务记录')
    note('九类管理业务记录前后逐字段相同')
    protectedReads = false
    await page.getByRole('button', { name: '退出登录', exact: true }).click()
    await page.locator('#login-password').waitFor({ state: 'visible' })
    assert.equal((await read(context, '/session')).authenticated, false)
    await page.reload()
    await page.locator('#login-password').waitFor({ state: 'visible' })
    note('通过实际退出按钮退出，刷新后会话仍未认证')
    await login()
    assert.deepEqual(await snapshot(context), before)
    await page.getByRole('button', { name: '退出登录', exact: true }).click()
    await page.locator('#login-password').waitFor({ state: 'visible' })
    note('实际页面重新登录成功，业务记录保持，最后退出专用浏览器会话')
    assert.equal(violations.length, 0, '浏览器发起非授权写入或跨环境请求，已阻断')
    assert.equal(pageErrors.length, 0, '浏览器发生脚本错误')
    const failedReads = responses.filter(row => row.验证会话期间 && !row.客户端断线中 && paths.some(p => row.路径 === '/api' + p) && row.状态 >= 400)
    assert.equal(failedReads.length, 0, '正常阶段真实管理 API 有失败响应')
    result.状态 = '通过'
    result.失败备份ID = task.id
    result.历史步骤数 = steps.length
    result.可见告警数 = before['/alerts'].length
  } catch (error) {
    result.状态 = '失败'
    result.原因 = scrub(error.message)
    if (page) await screenshot('first-failure.png').catch(() => {})
    process.exitCode = 1
  } finally {
    if (context) {
      await context.setOffline(false).catch(() => {})
      try {
        const payload = await read(context, '/session')
        if (payload.authenticated) {
          const response = await context.request.post(base + '/api/logout', { headers: { 'X-CSRF-Token': payload.csrf_token } })
          result.失败后专用会话退出HTTP = response.status()
        }
      } catch { result.专用会话退出清理 = '未确认，不能视为已退出' }
      await context.close()
    }
    if (browser) await browser.close()
    result.结束时间UTC = new Date().toISOString()
    result.受限证据目录 = folder
    result.违规请求 = violations
    result.脚本错误 = pageErrors
    save('result.json', result)
    save('responses.json', responses)
    fs.appendFileSync(path.join(root, 'results.jsonl'), JSON.stringify({ 类别: 'manager-ui-partial', 用例: '冻结界面上的真实浏览器历史与断线检查', 状态: result.状态,
      说明: result.状态 === '通过' ? '空闲页面部分检查通过，不代表运行中任务和整个管理类别' : result.原因, 时间: result.结束时间UTC,
      浏览器证据: path.relative(root, folder), 断言数: checks.length, 工具SHA256: result.工具SHA256 }) + '\n')
    console.log(JSON.stringify({ 浏览器检查: result.状态, 断言数: checks.length, 证据目录: folder, 原因: result.原因 }))
  }
}
main().catch(() => { console.error('浏览器工具初始化失败，未输出凭据'); process.exitCode = 1 })
