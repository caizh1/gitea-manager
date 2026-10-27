// 验证自动报告、历史补查和失败降级，接口使用固定样本且浏览器保持无头。
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), http = require('node:http')
const { chromium } = require('playwright')
const root = path.resolve(__dirname, '..'), output = path.join(root, 'work/failure-checks-20261027/ui')
fs.mkdirSync(output, { recursive: true })
const capabilities = ['dashboard', 'servers', 'backups', 'restore', 'settings', 'schedules', 'mirrors', 'statistics', 'svn_migrations', 'repository_policies', 'approval_rules', 'ai_reviews', 'commit_gates', 'access', 'alerts']
const session = { authenticated: true, needs_config: false, csrf_token: '固定测试令牌', organizations: [], capabilities,
  user: { id: 1, username: 'admin', display_name: '测试管理员', is_platform_admin: true, auth_source: 'local', must_change_password: false } }
const report = { version: 1, status: 'complete', summary: '仅检查保存记录，未执行重试。', checks: [
  { code: 'reference', state: 'confirmed', title: '当前单元证据', detail: 'F4H1L308 不在当前单元，匹配 10 个其他单元。' }] }
const rows = [1, 2].map(n => ({ id: 'job-'+n, number: n, repository: 1, status: 'failed', batch_protocol: 0,
  created: Date.now()/1000, updated: Date.now()/1000, error: '原始失败：证据编号无效',
  diagnostic: { error_code: 'BATCH_OUTPUT_INVALID', source: 'batch_result', reason: 'batch_evidence_unknown',
    session_id: 'rvw_'+n, ...(n===1 ? { automatic_checks: report } : {}) }, configuration: { model: '固定模型' },
  session: { id: 'rvw_'+n }, log_sync: { complete: true }, progress: {} }))
let requests = 0, fail = false, browser, server
const errors = [], writes = [], unknown = []
async function fixture(route) {
  const request=route.request(), p=new URL(request.url()).pathname
  if(request.method()!=='GET') writes.push(request.method()+' '+p)
  let value={}, status=200
  if(p==='/api/session') value=session
  else if(p==='/api/alerts/summary') value={active_count:0}
  else if(p==='/api/alerts') value=[]
  else if(p==='/api/ai-review/attention') value={summary:{pr_count:0},items:[],total:0}
  else if(p==='/api/ai-review/config') value={config:{enabled:true},health:[]}
  else if(p==='/api/ai-review/servers') value=[{id:1,name:'测试实例'}]
  else if(p==='/api/ai-review/repositories') value=[{id:1,repo_name:'test/code',server_name:'测试实例',enabled:true}]
  else if(p==='/api/ai-review/statistics') value=null
  else if(p==='/api/ai-review/tasks') value={items:rows,total:2}
  else if(p.endsWith('/failure-checks')) { requests++; value=fail?{error:'固定失败'}:report; status=fail?503:200 }
  else if(p.endsWith('/issues')) value={items:[],total:0}
  else if(p.endsWith('/events')) value={items:[],has_more:false}
  else if(/^\/api\/ai-review\/tasks\/job-[12]$/.test(p)) value=rows.find(row=>p.endsWith(row.id))
  else unknown.push(p)
  await route.fulfill({status,contentType:'application/json',body:JSON.stringify(value)})
}
async function main() {
  const dist=path.join(root,'frontend/dist')
  server=http.createServer((req,res)=>{ const candidate=path.resolve(dist,'.'+new URL(req.url,'http://localhost').pathname)
    const file=candidate.startsWith(dist+path.sep)&&fs.existsSync(candidate)&&fs.statSync(candidate).isFile()?candidate:path.join(dist,'index.html')
    res.writeHead(200,{'Content-Type':({'.js':'text/javascript','.css':'text/css','.html':'text/html'})[path.extname(file)]||'application/octet-stream'});fs.createReadStream(file).pipe(res) })
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve))
  browser=await chromium.launch({headless:true})
  const context=await browser.newContext({viewport:{width:1536,height:1000}})
  await context.route('**/api/**',fixture)
  const page=await context.newPage();page.setDefaultTimeout(8000);page.on('pageerror',error=>errors.push(error.message))
  await page.goto(`http://127.0.0.1:${server.address().port}/ai-reviews`)
  const row=n=>page.locator('.task-table-panel .el-table__body tr').filter({hasText:'#'+n})
  await row(1).getByRole('button',{name:'详情',exact:true}).click()
  const panel=page.getByTestId('failure-checks')
  await panel.getByText('F4H1L308 不在当前单元，匹配 10 个其他单元。').waitFor()
  assert.equal(requests,0)
  await page.getByRole('button',{name:'返回完整列表'}).click()
  await row(2).getByRole('button',{name:'详情',exact:true}).click()
  await panel.getByText('F4H1L308 不在当前单元，匹配 10 个其他单元。').waitFor()
  assert.equal(requests,1)
  fail=true;await panel.getByRole('button',{name:'重新检查'}).click()
  await panel.getByRole('alert').getByText('自动检查暂不可用，原始错误仍保留。').waitFor()
  assert.ok(await page.getByText('原始失败：证据编号无效',{exact:true}).count())
  fail=false;await panel.getByRole('button',{name:'重新检查'}).click()
  await panel.getByRole('alert').waitFor({state:'hidden'})
  await page.setViewportSize({width:390,height:1000})
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'窄屏页面不能横向溢出')
  await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true,animations:'disabled'})
  assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);assert.deepEqual(unknown,[])
  console.log('自动报告直接显示、历史错误自动补查、补查失败保留原始错误、只读请求和窄屏渲染通过。')
}
main().catch(error=>{console.error(error);process.exitCode=1}).finally(async()=>{await browser?.close();server?.close()})
