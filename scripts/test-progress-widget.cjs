/* 固定状态载荷验证；使用 Node 标准库，无需下载浏览器插件或依赖。 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { parse, effectiveStatus, extractFragment } = require('../backend/ai_review/assets/progress.js');
const identity = { id: 7, name: '合成/code', number: 1, path: '/gitea/合成/code/pulls/1' };
const payload = { version: 1, repository_id: 7, repository: '合成/code', number: 1,
 job: 'a'.repeat(32), generation: 2, job_generation: 2, sequence: 3, head: 'b'.repeat(40), current_head: 'b'.repeat(40),
 status: 'running', stage: 'analysis', detail: '分析修改单元 1/2', error: '', enabled: true,
 units_completed: 0, units_total: 2, received_at: 1, started_at: 2, finished_at: null, lease_until: 100,
 checked_at: 3, findings: null, review_id: null, review_fragment: '', coverage: null };
function row(p = payload, user = 9) { return { user: { id: user }, pull_request_url: 'http://gitea/gitea/合成/code/pulls/1',
 body: '<!-- ai-review:summary:v1 -->\n<!-- ai-review:progress:v1:' + Buffer.from(JSON.stringify(p)).toString('base64url') + ' -->' }; }
// URL 会规范化非 ASCII 路径，比较时使用同样的编码。
identity.path = new URL(row().pull_request_url).pathname;
test('中文状态、PR 身份和可信机器人', () => { assert.equal(parse(row(), identity, [9]).detail, payload.detail); });
test('拒绝恶意用户伪造的机器人标记', () => { assert.equal(parse(row(payload, 10), identity, [9]), null); });
test('拒绝其他仓库与其他 PR 的数据', () => {
 for (const changed of [{ repository_id: 8 }, { number: 2 }, { repository: '其他/code' }]) assert.equal(parse(row({ ...payload, ...changed }), identity, [9]), null);
 assert.equal(parse({ ...row(), pull_request_url: 'http://gitea/other/pulls/1' }, identity, [9]), null);
});
test('未知格式、异常计数和恶意结果链接不能展示', () => {
 for (const changed of [{ version: 2 }, { status: '__proto__' }, { units_completed: 9 }, { units_total: -1 }, { received_at: '今天' },
   { review_fragment: 'javascript:alert(1)' }, { head: '错误版本' }, { job_generation: 3 }]) assert.equal(parse(row({ ...payload, ...changed }), identity, [9]), null);
});
test('新提交覆盖旧完成状态，旧 Review 仍标记为历史', () => {
 assert.equal(effectiveStatus({ ...payload, status: 'succeeded' }, { state: 'open', head: { sha: 'c'.repeat(40) } }), 'outdated');
});
test('浏览器能在同步器中断时自行识别过期 Worker 租约', () => {
 assert.equal(effectiveStatus(payload, { state: 'open', head: { sha: payload.head } }, 101), 'interrupted');
 assert.equal(effectiveStatus({ ...payload, status: 'publishing' }, { state: 'open', head: { sha: payload.head } }, 101), 'publish_unknown');
});
test('关闭 PR 和禁用功能显示停止', () => {
 assert.equal(effectiveStatus(payload, { state: 'closed', head: { sha: payload.head } }), 'stopped');
 assert.equal(effectiveStatus({ ...payload, enabled: false }, { state: 'open', head: { sha: payload.head } }), 'stopped');
});

test('只提取最终服务器模板片段，不解析整个 PR 的图片和脚本', () => {
 const fragment = '<template id="ai-review-page-data" data-number="1"><div>已转义的 &lt;script&gt;</div></template>';
 assert.equal(extractFragment('<html><img src="https://example.invalid/image">&lt;template id="ai-review-page-data" &gt;'+fragment+'</html>'), fragment);
 assert.throws(()=>extractFragment('<html>登录页</html>'));
 assert.equal(effectiveStatus({...payload,head:'',status:'succeeded'}, {state:'open',head:{sha:payload.head}}), 'outdated');
});

test('前台定时更新、后台暂停、返回立即刷新与失败提示', async () => {
 const vm = require('node:vm'), fs = require('node:fs');
 const events = {}, timers = new Map(), elements = []; let serial = 0, requests = 0;
 function element(tag) { const node = { tag, dataset: {}, children: [], textContent: '', hidden: false,
   setAttribute() {}, append(...items) { this.children.push(...items); }, after() {},
   replaceChildren() { this.children = []; } }; elements.push(node); return node; }
 const heading = element('header');
 const document = { hidden: false, currentScript: { dataset: { reviewBots: '9', reviewBase: '/gitea' } },
   createElement: element, getElementById: () => null, querySelector: () => heading,
   addEventListener: (name, fn) => { events[name] = fn; } };
 const context = { document, window: { addEventListener: (name, fn) => { events[name] = fn; } },
   location: { pathname: '/gitea/owner/repo/pulls/1/files' }, URL, Date, Number, Object, TextDecoder, Uint8Array,
   AbortController, atob, setTimeout: (fn, delay) => { timers.set(++serial, {fn, delay}); return serial; },
   clearTimeout: id => timers.delete(id),
   fetch: async (path, options) => { requests++; assert.equal(path, '/gitea/owner/repo/pulls/1');
     assert.equal(options.credentials, 'same-origin'); assert.equal(options.redirect, 'error');
     return {ok: false, status: 503}; }
 };
 vm.runInNewContext(fs.readFileSync(require.resolve('../backend/ai_review/assets/progress.js'), 'utf8'), context);
 const flush = () => new Promise(resolve => setImmediate(resolve));
 await flush(); assert.equal(requests, 1);
 assert.ok(elements.some(n => n.textContent.includes('状态更新暂时失败')));
 assert.ok([...timers.values()].some(t => t.delay <= 5000 && t.delay > 4500));
 document.hidden = true; events.visibilitychange(); assert.equal(timers.size, 0);
 await flush(); assert.equal(requests, 1);
 document.hidden = false; events.visibilitychange(); await flush(); assert.equal(requests, 2);
 events.pagehide(); assert.equal(timers.size, 0);
 events.pageshow({persisted: true}); await flush(); assert.equal(requests, 3);
});
