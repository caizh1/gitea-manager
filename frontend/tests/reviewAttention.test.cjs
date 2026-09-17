// 共享待办的错误保留、请求合并及跨登录会话响应隔离。
const {test}=require('node:test')
const assert=require('node:assert/strict')
const fs=require('node:fs')
function harness() {
  const source=fs.readFileSync(require('node:path').join(__dirname,'../src/composables/useReviewAttention.js'),'utf8').replace(/^import .*$/gm,'').replace(/export /g,'')
  const timers=new Map(), listeners=new Map(), calls=[];let next=0
  const api={get:(path,options)=>new Promise((resolve,reject)=>calls.push({path,options,resolve,reject}))}
  const doc={hidden:false,addEventListener:(k,v)=>listeners.set(k,v),removeEventListener:k=>listeners.delete(k)}
  const hooks=new Function('reactive','api','document','setTimeout','clearTimeout',source+'\nreturn {startReviewAttention,stopReviewAttention,refreshReviewAttention,useReviewAttention}')
  return {...hooks(v=>v,api,doc,(f,ms)=>{timers.set(++next,{f,ms});return next},i=>timers.delete(i)),timers,listeners,calls,doc}
}
const data=count=>({data:{summary:{pr_count:count,blocked_task_count:count},items:[],worker:{available:true}}})
async function flush(){for(let i=0;i<8;i++)await Promise.resolve()}
test('多个消费者只建立一条五秒轮询，查询有超时边界',async()=>{const h=harness();h.startReviewAttention();h.startReviewAttention();assert.equal(h.calls.length,1);assert.equal(h.calls[0].options.timeout,5000);h.calls[0].resolve(data(2));await flush();assert.equal(h.timers.size,1);assert.equal([...h.timers.values()][0].ms,5000);h.stopReviewAttention();assert.equal(h.timers.size,1);h.stopReviewAttention();assert.equal(h.timers.size,0)})
test('刷新失败保留最后有效计数并设置错误',async()=>{const h=harness();const first=h.refreshReviewAttention();h.calls[0].resolve(data(2));await first;const second=h.refreshReviewAttention();h.calls[1].reject(new Error('隔离连接失败'));await second;assert.equal(h.useReviewAttention().summary.pr_count,2);assert.equal(h.useReviewAttention().error,'Review 待办状态暂时无法更新')})
test('同一轮并发只读刷新合并为一条请求',async()=>{const h=harness();const requests=Array.from({length:10},()=>h.refreshReviewAttention());assert.equal(h.calls.length,1);h.calls[0].resolve(data(1));await Promise.all(requests);assert.equal(h.useReviewAttention().summary.pr_count,1)})
test('退出再登录后旧响应不会覆盖新状态或增加轮询',async()=>{const h=harness();h.startReviewAttention();h.stopReviewAttention();h.startReviewAttention();assert.equal(h.calls.length,2);h.calls[1].resolve(data(3));await flush();h.calls[0].resolve(data(99));await flush();assert.equal(h.useReviewAttention().summary.pr_count,3);assert.equal(h.timers.size,1);h.stopReviewAttention();assert.equal(h.useReviewAttention().summary,null)})
test('页面恢复可见时立即请求并保持同一份状态',async()=>{const h=harness();h.doc.hidden=true;h.startReviewAttention();assert.equal(h.calls.length,0);h.doc.hidden=false;h.listeners.get('visibilitychange')();assert.equal(h.calls.length,1);h.calls[0].resolve(data(2));await flush();assert.equal(h.useReviewAttention().summary.pr_count,2);h.stopReviewAttention()})
