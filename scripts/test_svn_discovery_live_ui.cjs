// 真实隔离 Manager 的浏览器验收；默认只读，--write 在标记测试实例中迁移一个专用私有仓库。
const assert = require('node:assert/strict')
const fs = require('node:fs'), path = require('node:path'), http = require('node:http'), cp = require('node:child_process')
const { chromium } = require('playwright')
const root = path.resolve(__dirname, '..'), prefix = process.argv[2]
if (!/^gm-svn-[a-zA-Z0-9-]+$/.test(prefix || '')) throw Error('需要显式隔离实例名称')
const folder = path.join(root, 'work/svn-acceptance', prefix), state = JSON.parse(fs.readFileSync(path.join(folder, 'resources.json')))
const info = JSON.parse(cp.execFileSync('docker', ['inspect', prefix + '-manager']))[0]
assert.equal(info.Config.Labels['io.gitea-manager.test'], 'svn')
const upstream = Number(info.NetworkSettings.Ports['5000/tcp'][0].HostPort)
const server = http.createServer((req, res) => {
  if (req.url.startsWith('/api/')) {
    const proxy = http.request({ host: '127.0.0.1', port: upstream, method: req.method, path: req.url, headers: req.headers }, response => { res.writeHead(response.statusCode, response.headers); response.pipe(res) })
    proxy.on('error', () => { res.writeHead(502); res.end() }); req.pipe(proxy); return
  }
  const dist = path.join(root, 'frontend/dist'), candidate = path.resolve(dist, '.' + req.url.split('?')[0])
  const file = candidate.startsWith(dist + path.sep) && fs.existsSync(candidate) && fs.statSync(candidate).isFile() ? candidate : path.join(dist, 'index.html')
  res.writeHead(200, { 'Content-Type': ({ '.js': 'text/javascript', '.css': 'text/css' })[path.extname(file)] || 'text/html' }); fs.createReadStream(file).pipe(res)
})
async function main() {
  await new Promise(resolve => server.listen(process.argv.includes('--serve') ? 18746 : 0, '127.0.0.1', resolve))
  if (process.argv.includes('--serve')) { console.log('真实隔离预览：http://127.0.0.1:18746/svn-migrations'); return }
  const browser = await chromium.launch({ headless: true }), page = await browser.newPage({ viewport: { width: 1488, height: 1058 } })
  const errors = [], checks = [], base = `http://127.0.0.1:${server.address().port}`
  const check = name => { checks.push({ 检查: name, 通过: true }); console.log('通过：' + name) }
  page.on('pageerror', error => errors.push(error.message))
  try {
    await page.goto(base + '/svn-migrations')
    await page.getByLabel('账号', { exact: true }).fill('admin')
    await page.getByLabel('密码', { exact: true }).fill(state.manager_password_new || state.manager_password)
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await page.waitForURL('**/dashboard')
    await page.getByRole('menuitem', { name: '迁移工作台', exact: true }).click()
    await page.getByRole('heading', { name: '迁移工作台', exact: true }).waitFor()
    check('真实账号登录并进入迁移工作台')
    await page.getByRole('button', { name: '查看执行', exact: true }).first().click()
    await page.getByRole('heading', { name: '迁移顺序', exact: true }).waitFor()
    await page.reload(); await page.getByRole('heading', { name: '迁移顺序', exact: true }).waitFor()
    check('历史真实执行以独立页面显示且刷新保持同一任务')
    if (process.argv.includes('--write')) {
      await page.getByRole('menuitem', { name: '来源与仓库', exact: true }).click()
      const suffix = Date.now().toString(36), name = '浏览器闭环 ' + suffix
      await page.locator('.source-choice').first().waitFor()
      await page.getByRole('button', { name: '添加 SVN 来源', exact: true }).click()
      const source = page.getByRole('dialog', { name: '添加 SVN 来源' }), inputs = source.locator('input')
      for (const [i, value] of [name, 'http://source-http:8080', 'migration', state.svn_password].entries()) await inputs.nth(i).fill(value)
      await source.getByRole('button', { name: '连接并发现项目', exact: true }).click()
      const discovery = page.getByRole('dialog', { name: 'SVN 实例发现与项目纳管' })
      await discovery.getByRole('heading', { name: '发现 3 个可访问项目' }).waitFor({timeout:300000})
      assert.equal(await discovery.getByText('范围待确认').count(),0)
      await discovery.getByRole('checkbox',{name:'纳管项目 emmc_production',exact:true}).locator('xpath=ancestor::label').click()
      await discovery.getByRole('checkbox',{name:'纳管项目 project_a',exact:true}).locator('xpath=ancestor::label').click()
      await discovery.getByRole('button',{name:'查看目录'}).first().click()
      await discovery.getByText('test_spec/',{exact:true}).waitFor()
      await page.screenshot({path:path.join(folder,'discovery-selection-live.png'),fullPage:true})
      await discovery.getByRole('button',{name:'纳管所选的 2 个项目'}).click()
      check('真实 HTTP SVN 自动发现三个项目，多选两个直接纳管，普通目录不拆分')
      const plan = page.getByRole('dialog', { name: '创建迁移计划' })
      await plan.locator('input').first().fill(name)
      await plan.locator('.el-select__wrapper').first().click()
      await page.getByRole('option').filter({hasText:'SVN 隔离目标'}).click()
      await plan.getByRole('button', { name: '保存计划并预检', exact: true }).click()
      const row = page.getByRole('row').filter({ hasText: name })
      await row.getByRole('button', { name: /检查并执行|查看计划/, exact: true }).click()
      const preview = page.getByRole('dialog', { name: '迁移预览与停写确认' })
      const deadline = Date.now() + 1200000
      while (!(await preview.locator('.toolbar .el-tag').innerText()).includes('可执行')) { if (Date.now() > deadline) throw Error('真实预检未完成'); await page.waitForTimeout(500) }
      const start = preview.getByRole('button', { name: '按预览顺序迁移', exact: true })
      assert.equal(await start.isDisabled(), true)
      await preview.getByText('来源管理员已确认仓库清单完整，迁移账号可读取完整范围及历史', { exact: true }).click()
      await preview.getByText('所选源仓库已备份并停止写入；此次预检是在停写之后生成', { exact: true }).click()
      await start.click(); await page.waitForURL('**/runs/*')
      const currentStatus = page.locator('.page-heading .el-tag').first()
      while ((await currentStatus.innerText()) !== '成功') {
        const value = await currentStatus.innerText()
        if (/失败|待确认|部分完成/.test(value) || Date.now() > deadline) throw Error('浏览器迁移未完成：' + value)
        await page.waitForTimeout(1000)
      }
      check('真实停写确认、独立 Worker 迁移与自动内容核验成功')
      const runId = Number(new URL(page.url()).pathname.split('/').at(-1))
      const run = await (await page.request.get(base + '/api/svn-migrations/runs/' + runId)).json()
      assert.equal(run.snapshot.plan_name, name); assert.ok(run.snapshot.preflight_id)
      const spec=run.snapshot.repositories.find(r=>r.name==='emmc_production')
      const content=await page.request.get(`http://127.0.0.1:${Number(JSON.parse(cp.execFileSync('docker',['inspect',prefix+'-gitea']))[0].NetworkSettings.Ports['3000/tcp'][0].HostPort)}/api/v1/repos/${spec.owner}/${spec.name}/contents/test_spec/spec.txt?ref=svn-project`,{headers:{Authorization:'token '+state.gitea_token}})
      assert.equal(content.status(),200)
      check('目标 Gitea 同一仓库中真实回读 test_spec 完整内容，主干与分支标签一同保留')
      const preflightId = run.snapshot.preflight_id
      await page.screenshot({ path: path.join(folder, 'live-flow-r10.png'), fullPage: true, animations: 'disabled' })
      await page.goto(base + '/svn-migrations/preflights/' + preflightId + '?from=sources')
      await page.getByRole('heading', { name: '预检详情 #' + preflightId, exact: true }).waitFor()
      await page.getByRole('button', { name: '重新预检', exact: true }).click()
      await page.waitForURL(url => !url.pathname.endsWith('/' + preflightId))
      await page.getByRole('button', { name: '上一份预检', exact: true }).click()
      await page.getByRole('heading', { name: '预检详情 #' + preflightId, exact: true }).waitFor()
      check('重新预检生成独立记录，旧预检可经历史导航回看')
      await page.getByRole('button', { name: '返回上一页', exact: true }).click()
      await page.locator('.source-choice').filter({ hasText: name }).click()
      await page.getByRole('row').filter({ hasText: 'emmc_production' }).getByText('已迁移', { exact: true }).waitFor()
      check('来源页显示真实迁移事实，预检与迁移结果分列')
      await page.screenshot({ path: path.join(folder, 'live-source-r10.png'), fullPage: true, animations: 'disabled' })
    }
    assert.deepEqual(errors, []); check('真实后端页面无未捕获脚本异常')
  } catch (error) {
    await page.screenshot({ path: path.join(folder, 'discovery-live-failure.png'), fullPage: true, animations: 'disabled' })
    throw error
  } finally {
    fs.writeFileSync(path.join(folder, 'discovery-live-ui.json'), JSON.stringify({ 说明: '真实双 Gunicorn、SVN 与增强版 Gitea 的页面闭环', 检查: checks, 脚本异常: errors }, null, 2))
    await browser.close(); server.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; server.close() })
