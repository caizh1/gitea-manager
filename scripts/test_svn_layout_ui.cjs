// 生产构建的目录识别交互回归；模拟接口只验证页面，真实 SVN 另有集成验收。
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),http=require('node:http')
const {chromium}=require('playwright')
const root=path.resolve(__dirname,'..'),output=path.join(root,'work/svn-layout-ui')
fs.mkdirSync(output,{recursive:true})
const server=http.createServer((req,res)=>{const dist=path.join(root,'frontend/dist'),candidate=path.resolve(dist,'.'+req.url.split('?')[0]),file=candidate.startsWith(dist+path.sep)&&fs.existsSync(candidate)&&fs.statSync(candidate).isFile()?candidate:path.join(dist,'index.html');res.writeHead(200,{'Content-Type':({'.js':'text/javascript','.css':'text/css'})[path.extname(file)]||'text/html'});fs.createReadStream(file).pipe(res)})
const probes=new Map(),repositories=[],writes=[],checks=[],errors=[]
function layout(trunk='trunk',branches='branches',tags='tags'){return {trunk,branches,tags,default_branch:'main'}}
function result(url){
 const custom=url.includes('custom'),flat=url.includes('flat'),nested=url.includes('nested'),prefix=nested?'project/':''
 const selected=flat?layout('.','',''):custom?null:layout(prefix+'trunk',prefix+'branches',prefix+'tags')
 const directories=flat?[{path:'src',listed:true,files:1}]:custom?[{path:'production',listed:true,files:1},{path:'branches',listed:true,files:0},{path:'tags',listed:true,files:0}]:['trunk','branches','tags'].map(name=>({path:prefix+name,listed:true,files:name==='trunk'?1:0}))
 return {revision:20,uuid:'隔离样本',directories,entries:directories.map(row=>({name:row.path,kind:'dir'})),candidates:selected?[{layout:selected,reason:'实际目录证据'}]:[],limited:false,warnings:custom||flat?['需要确认迁移范围']:[],requests:3}
}
async function main(){await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const browser=await chromium.launch({headless:true});try{const page=await browser.newPage({viewport:{width:1280,height:950}});page.on('pageerror',e=>errors.push(e.message));await page.route('**/api/**',async route=>{const req=route.request(),url=new URL(req.url()),p=url.pathname,body=req.postDataJSON();let value={}
 if(p==='/api/session')value={authenticated:true,csrf_token:'隔离测试令牌',user:{id:1,username:'admin',is_platform_admin:true},organizations:[],capabilities:['svn_migrations'],needs_config:false}
 else if(p==='/api/alerts/summary')value={active_count:0}
 else if(p.endsWith('/sources'))value=[{id:1,name:'测试 SVN',slug:'test-svn',url:'http://example.test/svn',username:'reader',version:1,test:{}}]
 else if(p.endsWith('/repositories')){if(req.method()==='POST'){writes.push(body);repositories.push(...body.repositories.map((row,index)=>({...row,id:repositories.length+index+1,source_id:1,version:1,inventory:{}})))}value=repositories}
 else if(p.endsWith('/sources/1/layout-detections')){value=body.urls.map(url=>{const row={id:probes.size+1,url,source_id:1,status:'queued',result:{}};probes.set(row.id,row);return row})}
 else if(p.includes('/layout-detections/')){const row=probes.get(Number(p.split('/').at(-1))),data=result(row.url);value={...row,result:data,status:row.url.includes('failure')?'failed':data.warnings.length?'ambiguous':'ready',error:row.url.includes('failure')?'SVN 账号无法读取目录':''}}
 else if(p.endsWith('/status'))value={worker:{online:true},disk_free:1024**4,workspace_bytes:0}
 else if(['/plans','/runs','/targets'].some(s=>p.endsWith(s)))value=[]
 await route.fulfill({json:value,status:req.method()==='POST'&&p.endsWith('layout-detections')?202:200})})
 const base=`http://127.0.0.1:${server.address().port}`;await page.goto(base+'/svn-migrations/sources');await page.locator('.source-choice').filter({hasText:'测试 SVN'}).click()
 const open=async text=>{await page.getByRole('button',{name:'高级登记',exact:true}).click();const dialog=page.getByRole('dialog',{name:'批量登记仓库'});await dialog.locator('textarea').fill(text);await dialog.getByText('按指定布局迁移（高级）',{exact:true}).click();return dialog}
 const detect=async dialog=>{await dialog.getByRole('button',{name:'检测目录结构',exact:true}).click();await dialog.getByText(/已读取目录，请检查识别结果后保存/).waitFor()}
 let dialog=await open('standard http://example.test/svn/standard/\nnested http://example.test/svn/nested');assert.equal(await dialog.locator('.layout-form input').count(),0);await detect(dialog);assert.equal(await dialog.getByRole('button',{name:'保存仓库清单'}).isEnabled(),true)
 await dialog.locator('textarea').fill('standard http://example.test/svn/standard/\ninvalid-row');assert.equal(await dialog.getByRole('button',{name:'保存仓库清单'}).isDisabled(),true)
 await dialog.locator('textarea').fill('standard http://example.test/svn/standard/\nnested http://example.test/svn/nested');await detect(dialog);await dialog.getByRole('button',{name:'保存仓库清单'}).click();assert.equal(writes[0].repositories.length,2);assert.equal(writes[0].repositories[0].layout.trunk,'trunk');assert.equal(writes[0].repositories[1].layout.trunk,'project/trunk');assert.ok(writes[0].repositories.every(row=>row.detection_id));checks.push('标准和嵌套布局自动选择，批量逐仓库保存，URL 编辑后旧结果失效，无效行不能静默丢弃')
 dialog=await open('custom http://example.test/svn/custom');await detect(dialog);assert.equal(await dialog.getByRole('button',{name:'保存仓库清单'}).isDisabled(),true)
 for(const [label,value] of [['SVN 主干目录','production'],['SVN 分支目录','branches'],['SVN 标签目录','tags']]){const field=dialog.locator('.el-form-item').filter({hasText:label});const listId=await field.getByRole('combobox').getAttribute('aria-controls');await field.locator('.el-select__wrapper').click();await page.locator('#'+listId).getByRole('option',{name:value,exact:true}).click()}
 await dialog.getByText('已确认所选项目和迁移范围',{exact:true}).click();await dialog.getByRole('button',{name:'保存仓库清单'}).click();assert.equal(writes[1].repositories[0].layout.trunk,'production');checks.push('任意命名从真实目录选择，确认范围前禁止保存')
 dialog=await open('flat http://example.test/svn/flat');await detect(dialog);assert.equal(await dialog.getByRole('button',{name:'保存仓库清单'}).isDisabled(),true);await dialog.getByText('已确认所选项目和迁移范围',{exact:true}).click();for(const width of [1280,390]){await page.setViewportSize({width,height:950});await page.waitForTimeout(200);assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:path.join(output,`layout-${width}.png`),fullPage:true})}await dialog.getByRole('button',{name:'保存仓库清单'}).click();assert.equal(writes[2].repositories[0].layout.trunk,'.');checks.push('根目录布局确认后可保存，桌面与窄屏无横向溢出')
 dialog=await open('failure http://example.test/svn/failure');await detect(dialog);await dialog.getByText('SVN 账号无法读取目录',{exact:true}).waitFor();assert.equal(await dialog.getByRole('button',{name:'保存仓库清单'}).isDisabled(),true);checks.push('检测失败以红色错误提示且不能保存')
 assert.deepEqual(errors,[]);console.log('通过：'+checks.join('\n通过：'))
 }finally{fs.writeFileSync(path.join(output,'acceptance.json'),JSON.stringify({说明:'模拟接口的生产页面验证，真实 SVN 另行验收',检查:checks,脚本异常:errors},null,2));await browser.close();server.close()}}
main().catch(e=>{console.error(e);process.exitCode=1;server.close()})
