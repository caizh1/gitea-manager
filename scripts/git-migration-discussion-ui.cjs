// 后台浏览器完成真实差异确认，并核对目标评论、线程入口与合并后的只读状态。
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict')
const {chromium}=require('playwright')
const args=process.argv.slice(2)
const opt=k=>args[args.indexOf('--'+k)+1]
const manager=JSON.parse(fs.readFileSync(opt('manager-state'),'utf8'))
const target=JSON.parse(fs.readFileSync(opt('target-state'),'utf8'))
const out=opt('output'),evidence=path.join(out,'evidence.json')
const wait=async fn=>{const end=Date.now()+1200000;while(Date.now()<end){const v=fn();if(v)return v;await new Promise(r=>setTimeout(r,500))}throw Error('等待真实验收状态超时')}
;(async()=>{
 const browser=await chromium.launch({headless:true})
 const context=await browser.newContext({viewport:{width:1440,height:1000}})
 const page=await context.newPage(),errors=[];page.on('response',async r=>{if(r.url().includes('compatibility-acknowledgements'))console.log('确认接口状态',r.status())});page.on('pageerror',e=>errors.push(e.message))
 try{
  if(!fs.existsSync(path.join(out,'ui-confirmed.json'))){
  const initial=await wait(()=>{if(!fs.existsSync(evidence))return;const d=JSON.parse(fs.readFileSync(evidence));return d['预检查状态']==='blocked'&&d})
  const session=await (await context.request.get(manager.base+'/api/session')).json()
  const login=await context.request.post(manager.base+'/api/login',{headers:{'X-CSRF-Token':session.csrf_token},data:{password:manager.password}});assert(login.ok())
  await page.goto(manager.base+'/git-migrations/preflights/'+initial['预检查编号'],{waitUntil:'domcontentloaded'})
  await page.getByText('来源与目标的讨论规则差异',{exact:true}).waitFor()
  const start=page.getByRole('button',{name:'复检通过后创建正式迁移任务',exact:true});assert(await start.isDisabled())
  const confirm=page.getByRole('button',{name:'确认差异并重新预检',exact:true});assert(await confirm.isDisabled())
  await page.getByText('我已核对以上全部项目，同意目标采用 Gitea 原生合并规则',{exact:true}).click()
  assert(await page.getByRole('checkbox',{name:'我已核对以上全部项目，同意目标采用 Gitea 原生合并规则'}).isChecked())
  console.log('页面已勾选，提交差异确认');await confirm.click();console.log('已提交确认，等待预检')
  const limit=Date.now()+600000
  while(Date.now()<limit){const r=await (await context.request.get(manager.base+'/api/git-migrations/preflights/'+initial['预检查编号'])).json();if(r.status==='ready'){assert(r.compatibility.acknowledged);break}assert(!['failed','interrupted','cancelled'].includes(r.status));await page.waitForTimeout(500)}
  await page.reload({waitUntil:'domcontentloaded'});await page.getByText('已确认本批次行为差异；数据完整性仍须通过全部核验。').waitFor()
  await page.screenshot({path:path.join(out,'preflight-confirmed.png'),fullPage:true})
  fs.writeFileSync(path.join(out,'ui-confirmed.json'),JSON.stringify({'说明':'后台真实页面完成整批差异确认','结果':'通过'}))
  }
  const final=await wait(()=>{const d=JSON.parse(fs.readFileSync(evidence));return d['结果']==='通过'&&d})
  await page.goto(target.base+'/user/login',{waitUntil:'domcontentloaded'})
  await page.locator('input[name="user_name"]').fill('migration-admin');await page.locator('input[name="password"]').fill(target.password)
  await Promise.all([page.waitForURL(u=>!u.pathname.endsWith('/user/login')),page.getByRole('button',{name:/Sign In|登录/,exact:true}).click()])
  await page.goto(target.base+'/'+final['目标原生仓库名']+'/pulls/1',{waitUntil:'domcontentloaded'})
  const link=page.getByRole('link',{name:/迁移时：待解决/}).first();await link.waitFor()
  await page.screenshot({path:path.join(out,'discussion-after-merge.png'),fullPage:true})
  await link.click();await page.waitForLoadState('domcontentloaded')
  assert((await page.locator('body').innerText()).includes(final['来源快照'].thread_id))
  await page.screenshot({path:path.join(out,'discussion-history.png'),fullPage:true})
  const nonce=require('node:crypto').randomBytes(4).toString('hex')
  const adminHeaders={Authorization:'token '+target.token}
  const api=async(method,url,data)=>{const r=await context.request.fetch(target.base+'/api/v1'+url,{method,headers:adminHeaders,data});assert(r.ok(),url+' HTTP '+r.status());return r.status()===204?null:await r.json()}
  const rootID=final['预检查'].target.id
  let root=await api('GET','/governance/groups/'+rootID)
  const role=await api('POST','/governance/groups/'+rootID+'/roles',{name:'仅代码读取-'+nonce,base_role:10,abilities:['read_code'],revision:root.revision})
  const permissions=[]
  for(const [suffix,memberRole,customRole,expected] of [['code',10,role.id,404],['pull',15,0,200],['none',0,0,404]]){
   const username='history-'+suffix+'-'+nonce,password=require('node:crypto').randomBytes(24).toString('hex')
   const user=await api('POST','/admin/users',{username,password,email:username+'@example.invalid',must_change_password:false})
   if(memberRole){root=await api('GET','/governance/groups/'+rootID);await api('PUT','/governance/groups/'+rootID+'/members/'+user.id,{role:memberRole,custom_role_id:customRole,revision:root.revision})}
   const ctx=await browser.newContext(),tab=await ctx.newPage()
   await tab.goto(target.base+'/user/login');await tab.locator('input[name="user_name"]').fill(username);await tab.locator('input[name="password"]').fill(password)
   await Promise.all([tab.waitForURL(u=>!u.pathname.endsWith('/user/login')),tab.getByRole('button',{name:/Sign In|登录/,exact:true}).click()])
   const historyURL=target.base+'/'+final['目标路径']+'/migration-history/'+final['历史记录编号']
   const response=await ctx.request.get(historyURL);assert.equal(response.status(),expected,suffix+' 历史读取权限不符')
   if(suffix==='code'){const code=await ctx.request.get(target.base+'/'+final['目标路径']+'/src/branch/main/README.md');assert.equal(code.status(),200,'代码读取验证失败：'+(await code.text()).slice(0,200))}
   permissions.push({'角色':suffix,'历史页面HTTP':response.status()});await ctx.close()
  }
  fs.writeFileSync(path.join(out,'permissions.json'),JSON.stringify({'说明':'代码读取不能代替 PR 历史读取权限，PR 读取不要求代码权限','结果':'通过','角色':permissions},null,2))
  assert.equal(errors.length,0)
  fs.writeFileSync(path.join(out,'ui-acceptance.json'),JSON.stringify({'说明':'真实页面确认、已合并请求的来源待解决标识与线程入口，浏览器全程后台','结果':'通过','页面错误':errors},null,2))
  console.log('讨论历史后台页面验收通过')
 } catch(e) {await page.screenshot({path:path.join(out,'ui-failure.png'),fullPage:true});throw e} finally {await browser.close()}
})().catch(e=>{console.error(e.message);process.exitCode=1})
