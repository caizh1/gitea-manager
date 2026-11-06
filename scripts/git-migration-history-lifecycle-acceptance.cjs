// 在完成迁移的隔离实例验证结构化历史、原生编辑与删除、逐单元权限和分页。
const fs=require('node:fs'),cp=require('node:child_process'),assert=require('node:assert/strict'),path=require('node:path')
const {chromium}=require('playwright')
const arg=k=>process.argv[process.argv.indexOf('--'+k)+1]
const target=JSON.parse(fs.readFileSync(arg('target-state'))),manager=JSON.parse(fs.readFileSync(arg('manager-state')))
const fixture=JSON.parse(fs.readFileSync(arg('fixture'))),out=arg('output');fs.mkdirSync(out,{recursive:true})
const info=JSON.parse(cp.execFileSync('docker',['inspect',target.name]))[0];assert.equal(info.Config.Labels['io.gitea-manager.test'],'git-migration-receiver')
const sql=s=>JSON.parse(cp.execFileSync('docker',target.database==='postgres'
 ? ['exec',target.name+'-db','psql','-U','gitea','-d','gitea','-At','-v','ON_ERROR_STOP=1','-c',"SELECT COALESCE(json_agg(row_value),'[]'::json)::text FROM ("+s+") row_value"]
 : ['exec',target.name,'sqlite3','-json','/data/gitea/gitea.db',s],{encoding:'utf8'})||'[]')
const quote=v=>"'"+String(v).replaceAll("'","''")+"'"
const stored=JSON.parse(cp.execFileSync('docker',['exec',manager.name+'-backend','python','-c',"import sqlite3;print(sqlite3.connect('/app/data/gitea-manager.db').execute('select checkpoint_json from git_migration_runs where id=1').fetchone()[0])"],{encoding:'utf8'}))
const root=stored[String(fixture.project.id)],histories=sql('SELECT * FROM governance_migration_history WHERE repo_id='+root.repo_id+" AND kind='discussion_snapshot' ORDER BY id")
const parentID=h=>sql('SELECT target_id FROM governance_migration_receipt WHERE session_id='+h.session_id+' AND kind='+quote(JSON.parse(h.data).parent_kind)+' AND source_id='+quote(h.parent_id))[0].target_id
const noteID=(h,n)=>sql('SELECT target_id FROM governance_migration_receipt WHERE session_id='+h.session_id+' AND kind='+quote(n.kind)+' AND source_id='+quote(n.source_id))[0].target_id
;(async()=>{
 const browser=await chromium.launch({headless:true}),context=await browser.newContext({viewport:{width:1440,height:1100}}),page=await context.newPage(),errors=[]
 page.on('pageerror',e=>errors.push(e.message))
 const report={说明:'仅操作本轮最终包创建的隔离迁入仓库，完成其他固定基准后执行原生编辑和删除',结果:'执行中',场景:[]}
 const save=s=>{report.场景.push(s);fs.writeFileSync(path.join(out,'acceptance.json'),JSON.stringify(report,null,2))}
 const headers={Authorization:'token '+target.token}
 const api=async(method,url,data)=>{const r=await context.request.fetch(target.base+'/api/v1'+url,{method,headers,data});assert(r.ok(),method+' '+url+' HTTP '+r.status());return r.status()===204?null:await r.json()}
 try{
  await page.goto(target.base+'/user/login',{waitUntil:'domcontentloaded'});await page.locator('[name=user_name]').fill('migration-admin');await page.locator('[name=password]').fill(target.password)
  await Promise.all([page.waitForURL(u=>!u.pathname.endsWith('/user/login')),page.getByRole('button',{name:/Sign In|登录/,exact:true}).click()])
  const picked=[histories.find(h=>JSON.parse(h.data).parent_kind==='issue'),histories.find(h=>JSON.parse(h.data).notes.some(n=>n.kind==='code_comment'))];assert(picked.every(Boolean))
  const detail=h=>target.base+'/'+root.final_path+'/migration-history/'+h.id
  for(const h of picked){
   const raw=JSON.parse(h.data);await page.goto(detail(h),{waitUntil:'domcontentloaded'})
   const section=page.locator('section').filter({has:page.getByRole('heading',{name:'来源线程：'+raw.thread_id,exact:true})});await section.waitFor()
   const items=section.locator('li.item');assert.equal(await items.count(),raw.notes.length)
   for(let i=0;i<raw.notes.length;i++){
    const n=raw.notes[i],item=items.nth(i);assert((await item.innerText()).includes('来源评论 '+n.source_id))
    for(const [key,label] of [['resolvable','可解决'],['resolved','已解决'],['resolved_by','解决人'],['resolved_at','解决时间']]){
     const value=await item.locator('dt').filter({hasText:new RegExp('^'+label+'$')}).evaluate(e=>e.nextElementSibling.textContent)
     if(!(key in n))assert.equal(value,'来源未提供')
     else if(n[key]===null || n[key]==='')assert.equal(value,'来源返回空值')
     else if(typeof n[key]==='boolean')assert.equal(value,n[key]?'是':'否')
     else if(typeof n[key]==='string')assert.equal(value,n[key])
     else if(n[key].username)assert(value.includes(n[key].username))
    }
    const link=item.getByRole('link',{name:'查看目标评论',exact:true});assert.equal(await link.count(),1)
    assert((await context.request.get(target.base+await link.getAttribute('href'))).ok())
   }
   await page.screenshot({path:path.join(out,raw.parent_kind+'-structured.png'),fullPage:true})
  }
  save('普通与行内线程按来源顺序展示，逐项核验状态、解决人、时间、缺失值及目标链接')
  const first=picked[0],raw=JSON.parse(first.data),cid=noteID(first,raw.notes[0]),before=first.data
  await api('PATCH','/repos/'+root.final_path+'/issues/comments/'+cid,{body:'原生编辑后的正文，来源快照不得改变'})
  assert.equal(sql('SELECT data FROM governance_migration_history WHERE id='+first.id)[0].data,before)
  await api('DELETE','/repos/'+root.final_path+'/issues/comments/'+cid)
  await page.goto(detail(first),{waitUntil:'domcontentloaded'});await page.getByText('目标评论已删除',{exact:true}).first().waitFor()
  assert.equal(sql('SELECT data FROM governance_migration_history WHERE id='+first.id)[0].data,before)
  save('原生编辑与删除评论后来源快照未变，删除链接替换为明确提示')
  for(const [suffix,ability,expected] of [['issue','read_issues',[200,404]],['pull','read_pulls',[404,200]],['code','read_code',[404,404]],['none',null,[404,404]]]){
   const nonce=require('node:crypto').randomBytes(4).toString('hex'),username='history-'+suffix+'-'+nonce,password=require('node:crypto').randomBytes(24).toString('hex')
   const user=await api('POST','/admin/users',{username,password,email:username+'@example.invalid',must_change_password:false})
   if(ability){const repo=await api('GET','/repositories/'+root.repo_id);const owner=repo.full_name.split('/')[0],name=repo.name;const unit={read_issues:'repo.issues',read_pulls:'repo.pulls',read_code:'repo.code'}[ability];const team=await api('POST','/orgs/'+owner+'/teams',{name:suffix+'-'+nonce,permission:'read',units:[unit]});await api('PUT','/teams/'+team.id+'/members/'+username);await api('PUT','/teams/'+team.id+'/repos/'+owner+'/'+name)}
   const auth=await browser.newContext(),tab=await auth.newPage();await tab.goto(target.base+'/user/login');await tab.locator('[name=user_name]').fill(username);await tab.locator('[name=password]').fill(password)
   await Promise.all([tab.waitForURL(u=>!u.pathname.endsWith('/user/login')),tab.getByRole('button',{name:/Sign In|登录/,exact:true}).click()])
   for(let i=0;i<picked.length;i++)assert.equal((await auth.request.get(detail(picked[i]))).status(),expected[i],suffix)
   await auth.close()
  }
  save('议题、PR、仅代码及无权限账号分别按所属单元授权')
  for(const h of picked){const issue=sql('SELECT "index" FROM issue WHERE id='+parentID(h))[0];await api('DELETE','/repos/'+root.final_path+'/issues/'+issue.index);assert.equal((await context.request.get(detail(h))).status(),404);assert.equal(sql('SELECT data FROM governance_migration_history WHERE id='+h.id)[0].data,h.data)}
  let url=target.base+'/'+root.final_path+'/migration-history',pages=0
  while(url){const response=await page.goto(url,{waitUntil:'domcontentloaded'});assert.equal(response.status(),200);pages++;assert(pages<100);const next=page.getByRole('link',{name:'下一页',exact:true});url=await next.count()?new URL(await next.getAttribute('href'),url).href:null}
  save('通过原生接口删除议题与 PR 后详情隐藏、快照仍在，全部历史分页仍正常')
  report.结果='通过';report.历史分页数=pages;report.页面错误=errors;assert.equal(errors.length,0);fs.writeFileSync(path.join(out,'acceptance.json'),JSON.stringify(report,null,2));console.log('讨论历史生命周期验收通过')
 }catch(e){report.结果='失败';report.错误=e.message;fs.writeFileSync(path.join(out,'acceptance.json'),JSON.stringify(report,null,2));await page.screenshot({path:path.join(out,'failure.png'),fullPage:true});throw e}finally{await browser.close()}
})().catch(e=>{console.error(e.message);process.exitCode=1})
