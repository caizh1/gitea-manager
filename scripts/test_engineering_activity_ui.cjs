// 使用隔离 Gitea 采集生成的真实 Manager 数据验证看板交互。
const assert = require('node:assert/strict')
const path = require('node:path')
const { createRequire } = require('node:module')
const frontend = createRequire(path.join(__dirname, '../frontend/package.json'))
const { chromium } = frontend('playwright')

async function main() {
  const [base, output] = process.argv.slice(2)
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ viewport: { width: 1366, height: 1330 } })
    const errors = []
    page.on('pageerror', error => { errors.push(error.message); console.error(error.stack) })
    page.on('console', message => { if (message.type() === 'error' || message.type() === 'warning') console.error(message.text()) })
    const response = await page.request.get(base + '/api/session')
    const token = (await response.json()).csrf_token
    const login = await page.request.post(base + '/api/login', {
      data: { password: process.env.GM_ENGINEERING_FIXTURE_PASSWORD }, headers: { 'X-CSRF-Token': token },
    })
    assert.equal(login.status(), 200)
    await page.goto(base + '/engineering-activity')
    try {
      await page.getByRole('heading', { name: '研发上库', exact: true }).waitFor({ timeout: 8000 })
    } catch (error) {
      await page.screenshot({ path: path.join(output, 'dashboard-load-failure.png') })
      throw error
    }
    await page.getByRole('heading', { name: '研发 Top 10' }).waitFor()
    const today = page.waitForResponse(reply => {
      const url = new URL(reply.url())
      return url.pathname.endsWith('/dashboard') && url.searchParams.get('start') === url.searchParams.get('end')
    })
    await page.getByRole('button', { name: '今天', exact: true }).click()
    assert.equal((await today).status(), 200)
    await page.getByText('181', { exact: true }).first().waitFor()
    assert.equal(await page.locator('.ranking li').count(), 10)
    await page.screenshot({ path: path.join(output, 'dashboard.png'), fullPage: true })
    await page.getByRole('button', { name: '查看当日明细' }).click()
    await page.getByText('整组提交合入主线', { exact: true }).first().waitFor()
    await page.screenshot({ path: path.join(output, 'details.png') })
    await page.keyboard.press('Escape')
    await page.getByRole('dialog').waitFor({ state: 'hidden' })
    await page.locator('.el-select').filter({ has: page.getByRole('combobox', { name: '分支范围' }) }).click()
    await page.getByRole('option', { name: '进入主线', exact: true }).click()
    await page.getByText('90', { exact: true }).first().waitFor()
    await page.locator('.el-select').filter({ has: page.getByRole('combobox', { name: '排行指标' }) }).click()
    await page.getByRole('option', { name: '已解决 Bug', exact: true }).click()
    await page.waitForFunction(() => document.querySelectorAll('.ranking li').length === 2)
    await page.locator('.el-select').filter({ has: page.getByRole('combobox', { name: '研发团队' }) }).click()
    await page.getByRole('option', { name: 'engineering-a / firmware' }).click()
    await page.waitForFunction(() => document.querySelectorAll('.el-table__body tbody tr').length === 12)
    await page.setViewportSize({ width: 820, height: 900 })
    await page.screenshot({ path: path.join(output, 'dashboard-narrow.png'), fullPage: true })
    assert.equal(await page.locator('.engineering-dashboard').evaluate(el => el.scrollWidth <= el.clientWidth + 1), true)
    assert.deepEqual(errors, [])
    console.log('真实看板通过日期、排名、主线、团队、明细和窄屏验证')
  } finally {
    await browser.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
