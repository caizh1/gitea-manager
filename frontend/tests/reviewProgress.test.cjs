// PR 进度载荷的兼容性、归属校验与阻塞字段边界。
const { test } = require('node:test')
const assert = require('node:assert/strict')
const { parse, effectiveStatus } = require('../../backend/ai_review/assets/progress.js')
const identity = { id: 7, name: '测试/仓库', number: 1 }
const payload = { version: 1, repository_id: 7, repository: '测试/仓库', number: 1, status: 'queued', job: 'a'.repeat(32), head: 'b'.repeat(40), current_head: 'b'.repeat(40), generation: 2, job_generation: 2, sequence: 8, units_completed: 0, units_total: null, received_at: 1, started_at: null, finished_at: null, lease_until: null, checked_at: 2, enabled: true, detail: '等待管理员核对前次发布结果', error: '', stage: 'attention', findings: null, review_id: null, review_fragment: '', coverage: null }
function row(p, author=9) { return { user: {id:author}, body: '<!-- ai-review:summary:v1 -->\n<!-- ai-review:progress:v1:'+Buffer.from(JSON.stringify(p)).toString('base64url')+' -->' } }
test('兼容升级前未带待办字段的进度',()=>assert.ok(parse(row(payload),identity,[9])))
test('当前 PR 的真实阻塞字段可读取且保留 queued 状态',()=>{ const p={...payload,attention:{required:true,pending_count:2,number:1,reason:payload.detail}};assert.deepEqual(parse(row(p),identity,[9]),p);assert.equal(effectiveStatus(p,{state:'open',head:{sha:p.head}}),'queued') })
test('拒绝跨 PR 的待办载荷',()=>assert.equal(parse(row({...payload,attention:{required:true,pending_count:1,number:2,reason:'核对'}}),identity,[9]),null))
test('拒绝非可信机器人伪造待办',()=>assert.equal(parse(row(payload,8),identity,[9]),null))
test('拒绝非法待办计数与过长说明',()=>{for(const attention of [{required:true,pending_count:-1,number:1,reason:'核对'},{required:true,pending_count:1,number:1,reason:'长'.repeat(1001)}]) assert.equal(parse(row({...payload,attention}),identity,[9]),null)})
test('已关闭 PR 保留停止语义',()=>assert.equal(effectiveStatus(payload,{state:'closed',head:{sha:payload.head}}),'stopped'))
