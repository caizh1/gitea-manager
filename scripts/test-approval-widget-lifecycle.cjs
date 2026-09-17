/* 可控制响应顺序和页面事件的组件行为测试；不替代真实浏览器的布局验收。 */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../backend/approval/assets/widget.js'),'utf8');
function fixture(){
  let now=100000,next=1;
  const timers=new Map(),elements=[],requests=[],documentEvents={},windowEvents={};
  class Element{
    constructor(tag){this.tagName=tag;this.children=[];this.dataset={};this.events={};this.disabled=false;elements.push(this);}
    append(...children){this.children.push(...children);}
    after(child){this.following=child;}
    setAttribute(name,value){this[name]=value;}
    replaceChildren(...children){this.children=[...children];}
    addEventListener(name,fn){this.events[name]=fn;}
    set textContent(value){this.text=String(value);this.children=[];}
    get textContent(){return this.text||'';}
    set innerHTML(_){throw Error('组件不得将未信任内容作为HTML执行');}
  }
  const nativeHeading=new Element('header'),context=new Element('template'),script=new Element('script');
  context.id='gm-approval-context';context.dataset={repo:'1',number:'2',viewer:'3',head:'a'.repeat(40),closed:'false'};
  script.dataset={base:'',server:'4'};
  const document={hidden:false,currentScript:script,getElementById:id=>elements.find(e=>e.id===id),
    querySelector:()=>nativeHeading,createElement:tag=>new Element(tag),addEventListener:(name,fn)=>documentEvents[name]=fn};
  const window={addEventListener:(name,fn)=>windowEvents[name]=fn};
  const sandbox={document,window,AbortController,Date:class extends Date{static now(){return now;}},
    setTimeout:(fn,ms)=>{const id=next++;timers.set(id,{fn,at:now+ms});return id;},clearTimeout:id=>timers.delete(id),
    fetch:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}))};
  vm.runInNewContext(source,sandbox);
  const panel=elements.find(e=>e.id==='gm-approval-panel');
  return {panel,requests,timers,document,context,heading:nativeHeading,
    reload:()=>vm.runInNewContext(source,sandbox),
    async advance(ms){
      const until=now+ms;let count=0;
      while(true){const ready=[...timers].filter(([,v])=>v.at<=until).sort((a,b)=>a[1].at-b[1].at)[0];if(!ready)break;
        assert.ok(++count<100,'计时器没有有界退出');now=ready[1].at;timers.delete(ready[0]);ready[1].fn();await flush();}
      now=until;
    },
    visibility(hidden){document.hidden=hidden;documentEvents.visibilitychange();},
    pagehide:()=>windowEvents.pagehide(),pageshow:()=>windowEvents.pageshow({persisted:true}),
    refresh:()=>panel.children[4].events.click(),
    respond(index,value,status=200){requests[index].resolve({ok:status===200,status,text:async()=>JSON.stringify(value)});},
    rowCount:()=>panel.children[2].children[1].children.length};
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const payload=(changes={})=>({repository_id:1,number:2,viewer_id:3,head:'a'.repeat(40),revision:1,
  checked_at:100,valid_until:115,satisfied:true,grandfathered:false,applicable:true,mode:'shadow',
  total:{count:3,required:3},groups:[{name:'指定组',count:1,required:1,approved_ids:[7]}],
  approved:[{id:7,login:'审批人'}],...changes});
async function main(){
  const results=[];
  async function test(name,fn){await fn();results.push({场景:name,结果:'通过'});}
  await test('初始请求、重复脚本与原生区域保持',async()=>{
    const f=fixture();assert.equal(f.requests.length,1);assert.equal(f.requests[0].url,'/-/manager-approval/4/1/2');
    assert.equal(f.requests[0].options.credentials,'same-origin');assert.equal(f.requests[0].options.cache,'no-store');
    f.refresh();f.reload();assert.equal(f.requests.length,1);
    f.respond(0,payload());await flush();assert.equal(f.panel.dataset.state,'observed');assert.equal(f.rowCount(),2);
    assert.equal(f.heading.children.length,0);assert.equal(f.heading.following,f.panel);f.pagehide();assert.equal(f.timers.size,0);
  });
  await test('旧响应晚于新响应不能覆盖，隐藏页面取消轮询',async()=>{
    const f=fixture();f.respond(0,payload());await flush();f.refresh();assert.equal(f.requests.length,2);
    f.visibility(true);assert.equal(f.requests[1].options.signal.aborted,true);assert.equal(f.timers.size,0);
    f.visibility(false);assert.equal(f.requests.length,3);
    f.respond(2,payload({satisfied:false,total:{count:2,required:3}}));await flush();assert.equal(f.panel.dataset.state,'missing');
    f.respond(1,payload());await flush();assert.equal(f.panel.dataset.state,'missing');f.pagehide();assert.equal(f.timers.size,0);
  });
  await test('有效期结束立即清除通过，等待中的请求不续期',async()=>{
    const f=fixture();f.respond(0,payload());await flush();await f.advance(15000);
    assert.equal(f.panel.dataset.state,'unknown');assert.equal(f.rowCount(),0);assert.equal(f.requests.length,2);f.pagehide();
  });
  for(const status of [401,403,409,500,503])await test('HTTP '+status+'清除已有通过',async()=>{
    const f=fixture();f.respond(0,payload());await flush();f.refresh();f.respond(1,{},status);await flush();
    assert.equal(f.panel.dataset.state,'unknown');assert.equal(f.rowCount(),0);f.pagehide();
  });
  for(const [name,changes] of [['账户切换',{viewer_id:9}],['代码变化',{head:'b'.repeat(40)}],['旧规则版本',{revision:1}]]){
    await test(name+'不能显示为当前通过',async()=>{
      const f=fixture();f.respond(0,payload({revision:2}));await flush();f.refresh();f.respond(1,payload({revision:2,...changes}));await flush();
      assert.equal(f.panel.dataset.state,'unknown');assert.equal(f.rowCount(),0);f.pagehide();
    });
  }
  await test('返回页面缓存后只有一个新请求，关闭后没有遗留计时器',async()=>{
    const f=fixture();f.pagehide();assert.equal(f.requests[0].options.signal.aborted,true);assert.equal(f.timers.size,0);
    f.pageshow();assert.equal(f.requests.length,2);f.respond(1,payload());await flush();f.respond(0,payload({satisfied:false}));await flush();
    assert.equal(f.panel.dataset.state,'observed');f.pagehide();await f.advance(60000);assert.equal(f.requests.length,2);assert.equal(f.timers.size,0);
  });
  await test('规则与人员内容只能作为文本显示',async()=>{
    const f=fixture();f.respond(0,payload({name:'<img src=x onerror=alert(1)>',approved:[{id:7,login:'<script>不执行</script>'}]}));await flush();
    assert.equal(f.panel.dataset.state,'observed');assert.equal(f.panel.children[0].textContent,'<img src=x onerror=alert(1)>');f.pagehide();
  });
  await test('票数满足但原生请求修改时显示具体缺项，内容仅作为文本',async()=>{
    const f=fixture();f.respond(0,payload({satisfied:false,missing:['原生规则存在请求修改','<img src=x onerror=alert(1)>']}));await flush();
    assert.equal(f.panel.dataset.state,'missing');
    const rows=f.panel.children[2].children[1].children;
    assert.equal(rows[2].textContent,'尚未满足：原生规则存在请求修改');
    assert.equal(rows[3].textContent,'尚未满足：<img src=x onerror=alert(1)>');
    f.refresh();f.respond(1,payload());await flush();assert.equal(f.rowCount(),2);f.pagehide();
  });
  console.log(JSON.stringify({说明:'组件时序与资源生命周期固定测试；不是浏览器兼容或容量结果',通过数:results.length,结果:results},null,2));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
