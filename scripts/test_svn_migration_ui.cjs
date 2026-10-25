// 生产构建与模拟 API 的交互回归；真实迁移另由隔离集成验收覆盖。
const assert = require('node:assert/strict'), http = require('node:http')
const fs = require('node:fs'), path = require('node:path')
const root = path.resolve(__dirname, '..'), output = path.join(root, 'work/svn-ui-implementation-r1')
fs.mkdirSync(output, { recursive: true })
const records = { sources: [], repos: [], plans: [], runs: [], checks: [] }, writes = [], checks = []
const stamp = '2026-10-22T10:05:00', layouts = { trunk:'trunk',branches:'branches',tags:'tags',default_branch:'main' }
for (const [index, name] of ['研发 SVN','工具 SVN','归档 SVN'].entries()) {
  const id = index+1
  records.sources.push({id,name,slug:'source-'+id,url:`svn://source-${id}`,username:'migration',version:1,test:{status:'success'},password_set:true})
  for (const [i, name] of (index===0?['core-api','web-portal','archive-core','legacy-tools']:index===1?['build-tools','release-scripts','build-extra']:['archive-one','archive-two']).entries()) {
    records.repos.push({id:records.repos.length+1,source_id:id,name,url:`svn://source-${id}/${name}`,layout:layouts,version:1,inventory:{revision:87+i},archived:false})
  }
}
function addPlan(id,name,ids,status,checkId,current=true) {
  const repositories = ids.map(id=>({id,name:records.repos.find(r=>r.id===id).name}))
  const plan={id,name,server_id:1,config:{repositories,authors:[],target_ca_pem:'',excluded:[]},version:1,status,status_label:status==='ready'?'可执行':'待处理',preview_hash:'摘要-'+id}
  plan.preview={inputs:{plan_version:1},created_at:stamp,repositories:repositories.filter(r=>r.id!==4).map(r=>({...records.repos.find(x=>x.id===r.id),repository_id:r.id,source_name:'研发 SVN',owner:'svn-rd',revision:87,authors:['alice']})),selected:repositories.map(r=>({...records.repos.find(x=>x.id===r.id),repository_id:r.id,source_name:'研发 SVN'})),authors:[],issues:ids.includes(4)?[{repository_id:4,code:'externals',message:'检测到 svn:externals，本期不支持转换',url:'svn://source-1/legacy-tools'}]:[]}
  plan.preflight={id:checkId,plan_id:id,plan_name:name,plan_version:1,server_id:1,status,current,created_at:stamp,issue_count:plan.preview.issues.length}
  records.checks.push({...plan.preflight,config:structuredClone(plan.config),preview:structuredClone(plan.preview)})
  for(const r of repositories) records.repos.find(x=>x.id===r.id).preflight={...plan.preflight,issues:plan.preview.issues.filter(i=>i.repository_id===r.id),repository_status:r.id===4?'blocked':'ready'}
  records.plans.push(plan);return plan
}
const running = addPlan(1,'研发仓库切换',[1,2,3,5,6],'ready',79)
addPlan(2,'工具仓库迁移',[7,8],'ready',80)
addPlan(3,'历史项目归档',[3,4],'blocked',82)
addPlan(4,'补充仓库迁移',[2],'ready',76,false)
const snapshot={...structuredClone(running.preview),plan_name:running.name,preflight_id:79,full_read_confirmed:true,freeze:{confirmed:true,at:stamp}}
snapshot.repositories=running.config.repositories.map(r=>({...records.repos.find(x=>x.id===r.id),repository_id:r.id,source_name:r.id<5?'研发 SVN':'工具 SVN',revision:1842,owner:'svn-rd'}))
records.runs.push({id:28,plan_id:1,plan_name:running.name,plan_version:1,preflight_id:79,server_id:1,status:'running',status_label:'执行中',completed:2,total:5,created_at:stamp,updated_at:stamp,actor_name:'隔离管理员',snapshot,items:snapshot.repositories.map((r,i)=>({id:i+1,repository_id:r.repository_id,position:i,status:i<2?'success':i===2?'running':'pending',status_label:i<2?'成功':i===2?'执行中':'等待执行',stage:i<2?'completed':i===2?'verifying_history':'pending',detail:i===2?'已逐提交验证 128 / 360 个提交':'',result:{}}))})
records.repos[0].last_success={run_id:28};records.repos[0].migration={run_id:28,status:'success'}
const json=(res,value,code=200)=>{res.writeHead(code,{'Content-Type':'application/json'});res.end(JSON.stringify(value))}
const session={authenticated:true,csrf_token:'隔离页面令牌',user:{id:1,username:'admin',is_platform_admin:true},organizations:[],capabilities:['svn_migrations'],needs_config:false}
let loseReceipt = true
const server=http.createServer(async(req,res)=>{
 try {
  const url=new URL(req.url,'http://localhost'),p=url.pathname
  let raw='';for await(const part of req)raw+=part
  const body=raw?JSON.parse(raw):{}
  if(p.startsWith('/api/')) {
    if(p==='/api/session')return json(res,session)
    if(p==='/api/alerts/summary')return json(res,{active_count:0})
    if(!p.startsWith('/api/svn-migrations'))return json(res,{})
    const route=p.replace('/api/svn-migrations','')
    if(req.method!=='GET')writes.push({path:route,body})
    if(route==='/status')return json(res,{worker:{online:true},disk_free:1024**4,workspace_bytes:1024**3})
    if(route==='/targets')return json(res,[{id:1,name:'公司 Gitea',url:'http://example.test'}])
    if(route==='/sources'&&req.method==='POST'){const row={...body,id:records.sources.length+1,version:1,test:{}};records.sources.push(row);return json(res,row,201)}
    if(route==='/sources')return json(res,records.sources)
    if(/^\/sources\/\d+\/repositories$/.test(route)) {
      const source_id=Number(route.split('/')[2])
      if(req.method==='POST'){const rows=body.repositories.map((r,i)=>({...r,source_id,id:records.repos.length+i+1,inventory:{},archived:false}));records.repos.push(...rows);return json(res,rows,201)}
      return json(res,records.repos.filter(r=>r.source_id===source_id))
    }
    if(route==='/plans'&&req.method==='POST') {
      const row={...body,id:records.plans.length+1,config:{...body,authors:[]},version:1,status:'draft',status_label:'草稿',preview:{}}
      records.plans.unshift(row);return json(res,row,201)
    }
    if(/^\/plans\/\d+\/preflight$/.test(route)) {
      const row=records.plans.find(p=>p.id===Number(route.split('/')[2])),id=Math.max(...records.checks.map(c=>c.id))+1
      row.status='ready';row.status_label='可执行';row.preview_hash='新摘要-'+id
      row.preview={repositories:row.config.repositories.map(r=>({...r,repository_id:r.id,source_name:'测试来源',owner:'svn-test',revision:10,authors:['alice']})),authors:[],issues:[]}
      row.preflight={id,plan_id:row.id,plan_name:row.name,server_id:1,status:'ready',current:true,created_at:stamp}
      const previous=records.checks.filter(c=>c.plan_id===row.id).at(-1);if(previous)previous.next_id=id
      records.checks.push({...row.preflight,previous_id:previous?.id,config:structuredClone(row.config),preview:structuredClone(row.preview)});return json(res,row,202)
    }
    if(route==='/plans')return json(res,records.plans)
    if(/^\/plans\/\d+$/.test(route))return json(res,records.plans.find(p=>p.id===Number(route.split('/')[2])))
    if(/^\/preflights\/\d+$/.test(route))return json(res,records.checks.find(p=>p.id===Number(route.split('/')[2])))
    if(route==='/runs'&&req.method==='POST') {
      let row=records.runs.find(r=>r.request_key===body.request_key)
      if(!row){const plan=records.plans.find(p=>p.id===body.plan_id);row={id:29,request_key:body.request_key,plan_id:plan.id,plan_name:plan.name,plan_version:1,preflight_id:plan.preflight.id,server_id:1,status:'queued',status_label:'排队中',completed:0,total:plan.config.repositories.length,snapshot:{plan_name:plan.name,repositories:plan.preview.repositories},items:[]};records.runs.unshift(row)}
      if(loseReceipt){loseReceipt=false;return json(res,{error:'模拟回执丢失，请重试'},503)}
      return json(res,row,202)
    }
    if(route==='/runs')return json(res,records.runs)
    if(route.endsWith('/events'))return json(res,['来源身份与固定修订核对通过','历史转换完成，共 360 个提交','第 127 个提交内容与历史信息通过','正在验证第 128 个提交'].map((message,i)=>({id:i+1,item_id:3,created_at:stamp,message})).filter(e=>e.id>Number(url.searchParams.get('after')||0)))
    if(/^\/runs\/\d+$/.test(route))return json(res,records.runs.find(r=>r.id===Number(route.split('/')[2]))||{},200)
    return json(res,{error:'未定义的模拟接口'},404)
  }
  const dist=path.join(root,'frontend/dist'),candidate=path.resolve(dist,'.'+p)
  const file=candidate.startsWith(dist+path.sep)&&fs.existsSync(candidate)&&fs.statSync(candidate).isFile()?candidate:path.join(dist,'index.html')
  res.writeHead(200,{'Content-Type':({'.js':'text/javascript','.css':'text/css'})[path.extname(file)]||'text/html'});fs.createReadStream(file).pipe(res)
 }catch(e){json(res,{error:'模拟服务异常'},500);console.error(e)}
})
async function main(){
 await new Promise(resolve=>server.listen(process.argv.includes('--serve')?18745:0,'127.0.0.1',resolve))
 if(process.argv.includes('--serve')){console.log('设计对照预览：http://127.0.0.1:18745/svn-migrations');return}
 const {chromium}=require('playwright'),browser=await chromium.launch({headless:true})
 const page=await browser.newPage({viewport:{width:1488,height:1058}}),errors=[]
 page.on('pageerror',e=>errors.push(e.message))
 const base=`http://127.0.0.1:${server.address().port}`,check=name=>{checks.push({场景:name,结果:'通过'});console.log('通过：'+name)}
 try {
  await page.goto(base+'/svn-migrations');await page.getByRole('heading',{name:'迁移工作台',exact:true}).waitFor()
  await page.getByText('1 条有效阻断',{exact:true}).waitFor();check('工作台按计划显示阻断、停写待办与过期结果')
  await page.waitForTimeout(400);await page.screenshot({path:path.join(output,'workbench-1488.png'),fullPage:true,animations:'disabled'})
  await page.getByRole('button',{name:'查看预检详情',exact:true}).click();await page.getByRole('heading',{name:'预检详情 #82',exact:true}).waitFor()
  check('计划待办进入持久化预检详情')
  await page.waitForTimeout(400);await page.screenshot({path:path.join(output,'preflight-1488.png'),fullPage:true,animations:'disabled'})
  await page.getByRole('button',{name:'返回上一页',exact:true}).click()
  await page.getByRole('button',{name:'管理来源与仓库',exact:true}).click()
  await page.getByRole('button',{name:'研发 SVN 4',exact:true}).click()
  await page.locator('.el-table').getByText('已迁移',{exact:true}).waitFor();await page.getByText('需重新预检',{exact:true}).waitFor()
  check('来源页分别显示迁移事实和带所属计划的最近预检')
  await page.waitForTimeout(400);await page.screenshot({path:path.join(output,'sources-initial-1488.png'),fullPage:true,animations:'disabled'})
  await page.getByRole('row').filter({hasText:'legacy-tools'}).getByRole('button',{name:'查看预检',exact:true}).click()
  await page.getByRole('heading',{name:'预检详情 #82',exact:true}).waitFor();assert.match(page.url(),/from=sources/)
  await page.reload();await page.getByRole('heading',{name:'预检详情 #82',exact:true}).waitFor();check('来源与工作台引用同一预检编号且支持直接刷新')
  await page.getByRole('button',{name:'重新预检',exact:true}).click();await page.getByRole('heading',{name:'预检详情 #83',exact:true}).waitFor()
  await page.getByRole('button',{name:'上一份预检',exact:true}).click();await page.getByRole('heading',{name:'预检详情 #82',exact:true}).waitFor();assert.match(page.url(),/from=sources/)
  check('重新预检和历史翻阅保留来源页返回路径')
  await page.getByRole('button',{name:'返回上一页',exact:true}).click()
  await page.getByRole('button',{name:'研发 SVN 4',exact:true}).click()
  await page.getByRole('row').filter({hasText:'archive-core'}).locator('.el-checkbox').click()
  await page.getByRole('button',{name:'工具 SVN 3',exact:true}).click()
  await page.getByRole('row').filter({hasText:'build-tools'}).locator('.el-checkbox').click()
  await page.getByText('已选 2 个仓库 · 来自 2 个来源',{exact:true}).waitFor();check('切换来源保留勾选并统计跨来源范围')
  await page.getByRole('button',{name:'研发 SVN 4',exact:true}).click()
  await page.waitForTimeout(400);await page.screenshot({path:path.join(output,'sources-1488.png'),fullPage:true,animations:'disabled'})
  await page.getByRole('menuitem',{name:'迁移工作台',exact:true}).click()
  await page.getByRole('button',{name:'管理来源与仓库',exact:true}).click()
  await page.getByText('已选 2 个仓库 · 来自 2 个来源',{exact:true}).waitFor();check('返回工作台后跨来源选择仍保留')
  await page.getByRole('button',{name:'创建迁移计划',exact:true}).click()
  await page.getByRole('dialog',{name:'创建迁移计划',exact:true}).getByRole('button',{name:'保存计划并预检',exact:true}).click()
  await page.getByRole('row').filter({hasText:'SVN 内容迁移'}).getByRole('button',{name:'检查并执行',exact:true}).click()
  const preview=page.getByRole('dialog',{name:'迁移预览与停写确认'}),start=preview.getByRole('button',{name:'按预览顺序迁移',exact:true})
  assert.equal(await start.isDisabled(),true);check('未确认停写与完整读取不能执行')
  await preview.getByText('来源管理员已确认仓库清单完整，迁移账号可读取完整范围及历史',{exact:true}).click()
  await preview.getByText('所选源仓库已备份并停止写入；此次预检是在停写之后生成',{exact:true}).click()
  await start.click();await page.getByText('模拟回执丢失，请重试',{exact:false}).waitFor()
  await start.click();await page.waitForURL('**/runs/29');await page.getByRole('heading',{name:'SVN 内容迁移',exact:true}).waitFor()
  const requests=writes.filter(w=>w.path==='/runs');assert.equal(requests.length,2);assert.equal(requests[0].body.request_key,requests[1].body.request_key);assert.equal(records.runs.filter(r=>r.id===29).length,1)
  check('提交回执丢失后重试复用幂等标识且不重复创建任务')
  await page.goto(base+'/svn-migrations/runs/28');await page.getByRole('heading',{name:'archive-core',exact:true}).waitFor();await page.getByText('已验证 128 / 360 个提交',{exact:true}).waitFor()
  await page.waitForTimeout(400);await page.screenshot({path:path.join(output,'run-1488.png'),fullPage:true,animations:'disabled'});check('独立执行页显示来源、仓库队列、实际阶段和可测量进度')
  await page.reload();await page.getByText('已验证 128 / 360 个提交',{exact:true}).waitFor();check('执行页刷新恢复同一任务')
  for(const route of ['/svn-migrations','/svn-migrations/sources','/svn-migrations/preflights/82','/svn-migrations/runs/28']) {
    for(const width of [768,390]){await page.setViewportSize({width,height:950});await page.goto(base+route);await page.locator('.svn-center .surface').first().waitFor();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);await page.waitForTimeout(400);await page.screenshot({path:path.join(output,route.split('/').at(-1)+'-'+width+'.png'),fullPage:true,animations:'disabled'})}
  }
  check('四个页面在 390 与 768 宽度均无页面横向溢出')
  assert.deepEqual(errors,[]);check('页面无未捕获脚本异常')
 }finally{fs.writeFileSync(path.join(output,'ui-acceptance.json'),JSON.stringify({说明:'模拟接口的生产构建交互回归，不代表真实 SVN 内容迁移',检查:checks,脚本异常:errors},null,2));await browser.close();server.close()}
}
main().catch(e=>{console.error(e);process.exitCode=1;server.close()})
