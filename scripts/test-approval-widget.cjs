/* 审批页面协议固定测试：过期、账户切换、旧版本不能显示通过。 */
const assert = require('node:assert/strict');
const { valid, displayError } = require('../backend/approval/assets/widget.js');
const identity = { repo: 1, number: 2, viewer: 3, head: 'a'.repeat(40) };
const p = { repository_id: 1, number: 2, viewer_id: 3, head: identity.head, revision: 1,
  checked_at: 100, valid_until: 115, satisfied: true, grandfathered: false, mode: 'shadow' };
assert.equal(valid(p, identity, 101), true);
const bad = [ { viewer_id: 4 }, { repository_id: 9 }, { number: 3 }, { head: 'b'.repeat(40) },
  { revision: 0 }, { valid_until: 100 }, { checked_at: 999 }, { mode: '未知状态' }, { satisfied: 'true' } ];
for (const change of bad) assert.equal(valid({ ...p, ...change }, identity, 101), false);
assert.equal(valid({...p,closed:true},{...identity,head:'',closed:true},101),true);
assert.equal(valid({...p,closed:false},{...identity,head:'',closed:true},101),false);
assert.equal(valid({...p,closed:true},{...identity,head:'',closed:false},101),false);
assert.equal(displayError({name:'AbortError',message:'signal is aborted without reason'}),'审批服务响应超时，请稍后更新状态');
assert.equal(displayError(new TypeError('Failed to fetch')),'审批状态暂不可用，请稍后更新状态');
assert.equal(displayError(new SyntaxError('Unexpected token')),'审批数据格式异常，请刷新原生页面');
assert.equal(displayError(new Error('登录失效或没有权限，请刷新原生页面')),'登录失效或没有权限，请刷新原生页面');
console.log('审批页面协议：17 个断言通过');
