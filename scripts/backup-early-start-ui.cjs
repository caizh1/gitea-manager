// 后台真实浏览器验证固定副本的重收、丢弃确认和窄屏布局。
const fs = require('node:fs')
const path = require('node:path')
const http = require('node:http')
const assert = require('node:assert/strict')
const { createRequire } = require('node:module')
const root = path.resolve(__dirname, '..')
const { chromium } = createRequire(path.join(root, 'frontend/package.json'))('playwright')
const output = process.env.BACKUP_UI_EVIDENCE || path.join(root, 'work/backup-early-start/ui')
fs.mkdirSync(output, { recursive: true })
const server = http.createServer((req, res) => {
  const dist = process.env.BACKUP_UI_DIST || path.join(root, 'frontend/dist')
  const file = path.join(dist, new URL(req.url, 'http://localhost').pathname)
  const actual = fs.existsSync(file) && fs.statSync(file).isFile() ? file : path.join(dist, 'index.html')
  res.setHeader('Content-Type', ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html' })[path.extname(actual)] || 'application/octet-stream')
  res.end(fs.readFileSync(actual))
})
;(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
    const calls = []
    const row = { id: 1, source_server_name: '隔离源站', source_export_state: 'ready', source_export_size: 1073741824,
      source_restored_at: '2026-11-17T05:00:34Z', source_stopped_at: '2026-11-17T05:00:00Z',
      status: 'failed', error_msg: '验收注入传输中断', filename: 'sample.zip', can_retry_transfer: true, can_cleanup_export: true }
    const failed = { id: 2, filename: 'legacy.zip', status: 'failed', source_server_name: '隔离旧记录',
      error_msg: '固定导出元数据超过 16 MiB', failure_label: '固化源端备份副本', failure_at: '2026-11-18T01:00:00Z',
      recovery_pending: true,
      recovery: { state: 'waiting', action: '检查源 Gitea API 健康',
        error: '认证检查 /api/v1/user 返回 HTTP 401', source_state: 'running', next_retry_at: '2026-11-18T01:01:00Z' } }
    await page.route('**/api/**', async route => {
      const req = route.request(), url = new URL(req.url())
      let body = []
      if (url.pathname === '/api/session') body = { authenticated: true, needs_config: false, csrf_token: '隔离令牌', organizations: [],
        capabilities: ['backups', 'schedules', 'servers', 'settings', 'dashboard', 'ai_reviews'],
        user: { id: 1, username: '隔离管理员', is_platform_admin: true, auth_source: 'local', must_change_password: false } }
      else if (url.pathname === '/api/backups') body = [row, failed]
      else if (url.pathname === '/api/backups/2/steps') body = [
        { id: 1, status: 'failed', label: failed.failure_label, detail: failed.error_msg, step_key: 'freeze_export' },
        { id: 2, status: 'failed', label: failed.recovery.action, detail: failed.recovery.error, step_key: 'backup_recovery' },
      ]
      else if (url.pathname === '/api/schedules') body = [
        { id: 1, name: '隔离调度', enabled: true, last_status: 'failed', schedule_hour: 1, schedule_minute: 2 },
      ]
      else if (url.pathname === '/api/schedules/1/logs') body = [
        { steps: [{ stage: '备份', status: 'failed', detail: failed.error_msg, backup_id: failed.id, backup_outcome: failed }] },
      ]
      else if (url.pathname === '/api/settings') body = { host_ip: '127.0.0.1' }
      else if (url.pathname.endsWith('/summary')) body = { active_count: 0 }
      else if (url.pathname.endsWith('/attention')) body = { summary: {}, items: [] }
      else if (req.method() !== 'GET') { calls.push({ url: url.pathname, method: req.method(), data: req.postDataJSON() }); body = row }
      await route.fulfill({ json: body })
    })
    await page.goto(`http://127.0.0.1:${server.address().port}/backups`)
    await page.getByText('源站已恢复 · 停机 34 秒').waitFor()
    await page.getByRole('button', { name: '重新接收固定副本', exact: true }).click()
    await page.waitForTimeout(300)
    assert(calls.some(call => call.url.endsWith('/retry-transfer') && call.method === 'POST'))
    await page.getByRole('button', { name: '清理源端副本', exact: true }).click()
    await page.getByRole('button', { name: '确定', exact: true }).click()
    await page.waitForTimeout(300)
    assert(calls.some(call => call.url.endsWith('/source-export') && call.method === 'DELETE' && call.data.confirm_discard))
    await page.screenshot({ path: path.join(output, 'backup-desktop.png') })
    const oldRow = page.getByRole('row').filter({ hasText: 'legacy.zip' })
    await oldRow.getByText('等待重试').waitFor()
    await oldRow.getByText(failed.recovery.error, { exact: true }).waitFor()
    await oldRow.getByRole('button', { name: '展开详情' }).click()
    const dialog = page.getByRole('dialog', { name: '执行步骤' })
    await dialog.getByText(failed.error_msg, { exact: true }).first().waitFor()
    await dialog.getByText(failed.recovery.error, { exact: true }).first().waitFor()
    await dialog.getByRole('button', { name: '关闭执行步骤' }).click()
    await page.setViewportSize({ width: 390, height: 844 })
    await page.screenshot({ path: path.join(output, 'backup-mobile.png') })
    const overlap = await page.locator('.backup-actions .backup-action').evaluateAll(nodes => {
      const rects = nodes.map(node => node.getBoundingClientRect())
      return rects.some((a, i) => rects.some((b, j) => i !== j && a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top))
    })
    assert.equal(overlap, false)
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto(`http://127.0.0.1:${server.address().port}/schedules`)
    await page.getByRole('row').filter({ hasText: '隔离调度' }).locator('.el-table__expand-icon').click()
    await page.getByText(failed.error_msg, { exact: true }).waitFor()
    await page.getByText(failed.recovery.error, { exact: true }).waitFor()
    await page.getByText('源 Gitea 容器已运行，API 健康待确认', { exact: true }).waitFor()
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify({
      结果: '通过', 检查: ['重收请求', '明确丢弃确认', '停机耗时', '窄屏图标不重叠', '备份首错', '收尾认证失败', '定时任务历史'],
    }, null, 2))
    console.log('后台浏览器固定副本操作检查通过')
  } finally { await browser.close(); server.close() }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1 })
