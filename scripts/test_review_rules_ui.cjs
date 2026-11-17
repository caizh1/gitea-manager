// 使用生产构建和隔离接口样本验证规则正文、权限状态与窄屏展示。
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const http = require('node:http')
const { createRequire } = require('node:module')
const root = path.resolve(__dirname, '..')
const { chromium } = createRequire(path.join(root, 'frontend/package.json'))('playwright')
const output = path.join(root, 'work/review-rules-ui')
fs.mkdirSync(output, { recursive: true })
const rules = { status: 'loaded', path: '.gitea/review-rules.md', source_commit: 'a'.repeat(40), sha256: 'b'.repeat(64), submitted: true }
const content = '# 自定义规则\n<script>window.rulesExecuted=true</script>\n<img src="https://example.invalid/secret">\n- 用户自己的约束\n' + '中文'.repeat(200)
const task = { id: 'rules-test', repository: 1, number: 1, status: 'succeeded', created: 1700000000, updated: 1700000000, review_rules: rules,
  configuration: { engine: 'codex_cli', model: '隔离模型' }, log_sync: { complete: true }, result: { findings: [], report_markdown: 'VERDICT: PASS', coverage: { complete: true } } }
let ruleStatus = 'loaded', rulesReads = 0
const errors = []
async function main() {
  const dist = path.join(root, 'frontend/dist')
  const server = http.createServer((req, res) => {
    const candidate = path.resolve(dist, '.' + new URL(req.url, 'http://localhost').pathname)
    const file = candidate.startsWith(dist + path.sep) && fs.existsSync(candidate) && fs.statSync(candidate).isFile() ? candidate : path.join(dist, 'index.html')
    res.writeHead(200, { 'Content-Type': ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html' })[path.extname(file)] || 'application/octet-stream' })
    fs.createReadStream(file).pipe(res)
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
    page.on('pageerror', e => errors.push(e.message))
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url()), p = url.pathname
      let value = {}
      if (p === '/api/session') value = { authenticated: true, needs_config: false, organizations: [], capabilities: ['ai_reviews', 'alerts'], user: { id: 1, username: 'admin', is_platform_admin: true, auth_source: 'local' } }
      else if (p === '/api/ai-review/config') value = { config: { enabled: true, engine: 'codex_cli' }, health: [] }
      else if (p === '/api/ai-review/repositories') value = [{ id: 1, repo_name: '隔离/规则仓库', server_name: '隔离实例', enabled: true }]
      else if (p === '/api/ai-review/servers') value = []
      else if (p === '/api/ai-review/tasks') value = { items: [task], total: 1 }
      else if (p.endsWith('/rules')) { rulesReads++; value = { ...rules, content, source_url: 'https://example.invalid/src/commit/' + rules.source_commit + '/' + rules.path } }
      else if (p === '/api/ai-review/tasks/rules-test') value = { ...task, review_rules: { ...rules, status: ruleStatus, sha256: ruleStatus === 'loaded' ? rules.sha256 : null } }
      else if (p.endsWith('/issues') || p.endsWith('/events') || p.endsWith('/context-reads')) value = { items: [], total: 0 }
      else if (p.endsWith('/attention')) value = { items: [], total: 0, summary: { pr_count: 0 } }
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify(value) })
    })
    const base = `http://127.0.0.1:${server.address().port}`
    for (const [status, label] of [['loaded', '已加载并提交模型'], ['missing', '未配置'], ['empty', '内容为空'], ['failed', '加载失败'], ['legacy', '历史任务不适用']]) {
      ruleStatus = status
      await page.goto(base + '/ai-reviews')
      await page.getByRole('button', { name: '详情', exact: true }).click()
      const panel = page.getByTestId('review-rules')
      // 切到承载执行配置的页签，保持与实际用户入口一致。
      const tabs = page.locator('.review-detail').getByRole('tab')
      for (let i = 0; i < await tabs.count(); i++) {
        await tabs.nth(i).click()
        if (await panel.isVisible()) break
      }
      await panel.getByText(label, { exact: false }).waitFor()
      if (status === 'loaded') {
        assert.equal(rulesReads, 0)
        await panel.getByRole('button', { name: '查看冻结规则' }).click()
        await panel.locator('pre').waitFor()
        assert.equal(await panel.locator('pre').textContent(), content)
        assert.equal(await page.evaluate(() => window.rulesExecuted), undefined)
        assert.equal(await panel.locator('img,script').count(), 0)
        assert.match(await panel.getByRole('link').getAttribute('href'), /\/src\/commit\/a{40}\//)
        for (const width of [1440, 390]) {
          await page.setViewportSize({ width, height: 1000 })
          assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
          await panel.screenshot({ path: path.join(output, `rules-${width}.png`) })
        }
      }
    }
    assert.deepEqual(errors, [])
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify({ 结果: '通过', 状态数量: 5, 正文请求: rulesReads, 脚本执行: false, 视口: [1440, 390] }, null, 2))
    console.log('规则状态、冻结正文、HTML 转义和窄屏布局通过')
  } finally {
    await browser.close()
    server.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
