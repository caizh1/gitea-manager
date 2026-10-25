// 修订版真实生产页面的交互验收；接口模拟与真实 SVN 验收分别记录。
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict')
const {chromium}=require('playwright')
const root=path.resolve(__dirname,'..'),out=path.join(root,process.argv.includes('--insecure-origin')?'work/svn-target-groups-20261024/ui-http':'work/svn-target-groups-20261024/ui');fs.mkdirSync(out,{recursive:true})
const server=http.createServer((req,res)=>{const dist=path.join(root,'frontend/dist'),p=path.resolve(dist,'.'+req.url.split('?')[0]),file=p.startsWith(dist+path.sep)&&fs.existsSync(p)&&fs.statSync(p).isFile()?p:path.join(dist,'index.html');res.writeHead(200,{'Content-Type':({'.js':'text/javascript','.css':'text/css'})[path.extname(file)]||'text/html'});fs.createReadStream(file).pipe(res)})
const checks=[],errors=[],sources=[{id:1,name:'研发 SVN',slug:'research',url:'http://example.test',username:'reader',version:1,test:{}}],repos=[];let mode='ready',serial=0,enrolled=[]
const projects=['emmc_production','project_a','project_b'].map((name,i)=>({key:String(i),name,label:name,url:'http://example.test/'+name,entries:['trunk','branches','tags','test_spec'].map(name=>({name,kind:'dir'}))}))
async function main(){await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({headless:true,args:process.argv.includes('--insecure-origin')?['--host-resolver-rules=MAP manager-http.test 127.0.0.1','--no-proxy-server']:[]});try{const page=await browser.newPage({viewport:{width:1488,height:1058}});page.on('pageerror',e=>errors.push(e.message));await page.route('**/api/**',async route=>{const request=route.request(),url=new URL(request.url()),p=url.pathname,body=request.postDataJSON();let data={}
 if(p==='/api/session')data={authenticated:true,csrf_token:'测试会话',user:{id:1,username:'admin',is_platform_admin:true},organizations:[],capabilities:['svn_migrations'],needs_config:false}
 else if(p==='/api/alerts/summary')data={active_count:0}
 else if(p.endsWith('/sources')){if(request.method()==='POST'){sources.push({...body,id:2,version:1,test:{}});data=sources.at(-1)}else data=sources}
 else if(p.endsWith('/repositories'))data=repos
 else if(p.endsWith('/discoveries'))data={id:++serial,status:'queued',result:{}}
 else if(p.includes('/discoveries/'))data={id:serial,status:mode,result:{connected:mode!=='failed',projects:mode==='ready'?projects:[],message:'服务器未提供项目列表'},error:mode==='failed'?'连接失败：账号或密码错误':''}
 else if(p.endsWith('/enroll')){enrolled=body.keys;data=projects.filter(r=>body.keys.includes(r.key)).map((r,i)=>({...r,id:i+1,source_id:1,version:1,layout:{project:true,trunk:'.',branches:'',tags:'',default_branch:'main'},inventory:{}}));repos.splice(0,repos.length,...data)}
 else if(p.endsWith('/groups/query'))data={groups:[{id:42,name:'固件研发',full_path:'firmware',parent_id:0,selectable:true,can_create_group:true}],next_id:0}
 else if(p.endsWith('/paths'))data={group:{id:42,name:'固件研发',full_path:'firmware',parent_id:0,selectable:true,can_create_group:true},repositories:body.names.map(name=>({name,path:'firmware/'+name,available:true,message:'路径可用'}))}
 else if(p.endsWith('/targets'))data=[{id:1,name:'研发 Gitea',url:'http://gitea.test'}]
 else if(p.endsWith('/status'))data={worker:{online:true}}
 else if(p.endsWith('/plans')||p.endsWith('/runs'))data=[]
 await route.fulfill({json:data})})
 await page.goto(`http://${process.argv.includes('--insecure-origin')?'manager-http.test':'127.0.0.1'}:${server.address().port}/svn-migrations/sources`)
 if(process.argv.includes('--insecure-origin')){
  assert.deepEqual(await page.evaluate(()=>[isSecureContext,typeof crypto.randomUUID,typeof crypto.getRandomValues]),[false,'undefined','function'])
  for(let index=0;index<3;index++){
   await page.getByRole('button',{name:'添加 SVN 来源',exact:true}).click()
   const form=page.getByRole('dialog',{name:'添加 SVN 来源'});await form.waitFor({timeout:3000})
   assert.match(await form.locator('details input').first().inputValue(),/^svn-[0-9a-f]{16}$/)
   await form.getByRole('button',{name:'取消',exact:true}).click();await form.waitFor({state:'hidden'});await page.waitForTimeout(350)
  }
  checks.push('真实普通 HTTP 域名中 randomUUID 不可用时仍可反复打开添加来源，标识使用安全随机值')
 }
 await page.locator('.source-choice').filter({hasText:'研发 SVN'}).click()
 const open=async()=>{await page.getByRole('button',{name:'连接并发现项目',exact:true}).click();await page.getByRole('heading',{name:'发现 3 个可访问项目'}).waitFor();return page.getByRole('dialog',{name:'SVN 实例发现与项目纳管'})}
 let dialog=await open();assert.equal(await dialog.getByText('范围待确认').count(),0);assert.equal(await dialog.getByRole('button',{name:'纳管所选的 0 个项目'}).isDisabled(),true)
 await dialog.getByRole('checkbox',{name:'纳管项目 emmc_production',exact:true}).locator('xpath=ancestor::label').click();await dialog.getByRole('checkbox',{name:'纳管项目 project_a',exact:true}).locator('xpath=ancestor::label').click();await dialog.getByRole('button',{name:'查看目录'}).first().click();await dialog.getByText('test_spec/',{exact:true}).waitFor()
 for(const width of [1488,390]){await page.setViewportSize({width,height:1100});await page.waitForTimeout(100);assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:path.join(out,`selection-${width}.png`),fullPage:true})}
 await page.setViewportSize({width:1488,height:1058});await dialog.getByRole('button',{name:'纳管所选的 2 个项目'}).click();await page.waitForURL('**/plans/new?**');const plan=page.getByRole('region',{name:'创建迁移计划'});await plan.waitFor();assert.deepEqual(enrolled,['0','1']);await plan.getByRole('textbox',{name:'计划名称',exact:true}).fill('研发项目迁移');await plan.getByText('已选择 2 个 SVN 项目，指定它们在 Gitea 中的位置。',{exact:true}).waitFor();await plan.locator('.el-select__wrapper').nth(0).click();await page.getByRole('option',{name:'研发 Gitea'}).click();await plan.locator('.el-select__wrapper').nth(1).click();await page.getByRole('option',{name:'固件研发 · firmware'}).click();await plan.getByRole('button',{name:'保存计划并预检'}).waitFor();await page.waitForFunction(()=>[...document.querySelectorAll('button')].some(b=>b.textContent.includes('保存计划并预检')&&!b.disabled));assert.equal(await plan.getByText('firmware/emmc_production',{exact:true}).count(),1);assert.equal(await plan.locator('.svn-steps li.current').innerText(),'2\n选择目标');assert.equal(await plan.getByText('emmc_production-test_spec').count(),0);await page.screenshot({path:path.join(out,'plan-1488.png'),fullPage:true});checks.push('从三个项目多选两个直接纳管并进入迁移计划；无重复范围确认；test_spec 保持项目归属；桌面和窄屏可操作')
 await plan.getByRole('button',{name:'上一步',exact:true}).click();await page.locator('.source-choice').filter({hasText:'研发 SVN'}).click();mode='unavailable';await page.getByRole('button',{name:'连接并发现项目',exact:true}).click();dialog=page.getByRole('dialog',{name:'SVN 实例发现与项目纳管'});await dialog.getByText('服务器未提供项目列表',{exact:true}).waitFor();await dialog.getByRole('button',{name:'补充项目地址',exact:true}).click();await dialog.getByRole('textbox',{name:'补充项目地址',exact:true}).fill('http://example.test/emmc_production');assert.equal(await dialog.getByRole('button',{name:'识别此项目'}).isEnabled(),true);await page.screenshot({path:path.join(out,'unavailable.png'),fullPage:true});checks.push('列表不可用显示备用项目地址入口，连接失败独立显示红色错误')
 await dialog.getByRole('button',{name:'关闭',exact:true}).click();mode='failed';await page.getByRole('button',{name:'连接并发现项目',exact:true}).click();dialog=page.getByRole('dialog',{name:'SVN 实例发现与项目纳管'});await dialog.getByText('连接失败：账号或密码错误',{exact:true}).waitFor();assert.equal(await dialog.locator('.el-alert--error').count(),1);await dialog.getByRole('button',{name:'关闭',exact:true}).click()
 mode='ready';await page.getByRole('button',{name:'添加 SVN 来源',exact:true}).click();const source=page.getByRole('dialog',{name:'添加 SVN 来源'});await source.locator('input').nth(0).fill('新增实例');await source.locator('input').nth(1).fill('http://new.test');await source.locator('input').nth(2).fill('reader');await source.locator('input').nth(3).fill('测试口令');await source.getByRole('button',{name:'连接并发现项目',exact:true}).click();await page.getByRole('dialog',{name:'SVN 实例发现与项目纳管'}).waitFor();checks.push('新实例保存后直接发现；英文标识自动生成，无需手填项目 URL')
 assert.deepEqual(errors,[]);console.log(checks.join('\n'))
 }finally{await browser.contexts()[0]?.pages()[0]?.screenshot({path:path.join(out,'last-state.png'),fullPage:true});fs.writeFileSync(path.join(out,'acceptance.json'),JSON.stringify({说明:'生产前端模拟接口验收，与真实协议验收分别记录',检查:checks,异常:errors},null,2));await browser.close();server.close()}}
main().catch(e=>{console.error(e);process.exitCode=1;server.close()})
