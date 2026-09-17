import test from 'node:test'
import assert from 'node:assert/strict'
import { createCandidateSearch, forceMode, pushMode, setForceMode, setPushMode, setOrganization, teamFields, toApiRule, toEditorRule } from '../src/components/policyForm.js'

const lists = ['push_whitelist_usernames', 'push_whitelist_teams', 'force_push_allowlist_usernames', 'force_push_allowlist_teams', 'merge_whitelist_usernames', 'merge_whitelist_teams', 'approvals_whitelist_username', 'approvals_whitelist_teams', 'status_check_contexts']
const bools = 'enable_push enable_push_whitelist push_whitelist_deploy_keys enable_force_push enable_force_push_allowlist force_push_allowlist_deploy_keys enable_merge_whitelist enable_status_check enable_approvals_whitelist block_on_rejected_reviews block_on_official_review_requests block_on_outdated_branch dismiss_stale_approvals ignore_stale_approvals require_signed_commits block_admin_merge_override'.split(' ')
test('全部 30 字段和组织原样导入保存无变化，并且编辑不污染原对象', () => {
  const source = { rule_name: 'main', priority: 4, required_approvals: 2, protected_file_patterns: 'secret/**', unprotected_file_patterns: 'docs/**', team_org: 'group' }
  bools.forEach(k => source[k] = true); lists.forEach(k => source[k] = ['one', 'two'])
  const editor = toEditorRule(source, lists)
  assert.equal(Object.keys(source).length, 31)
  assert.deepEqual(toApiRule(editor), source)
  editor.push_whitelist_usernames.push('three'); assert.equal(source.push_whitelist_usernames.length, 2)
})
test('推送和强推独立映射，与 Gitea 原生页面一致', () => {
  for (const push of ['none', 'all', 'list']) for (const force of ['none', 'all', 'list']) {
    const r = {}; setPushMode(r, push); setForceMode(r, force)
    assert.equal(pushMode(r), push); assert.equal(forceMode(r), force)
    setPushMode(r, 'none'); assert.equal(forceMode(r), force)
  }
})
test('切换组织只清空团队，原组织再次确认不改名单', () => {
  const r = { team_org: 'old', push_whitelist_usernames: ['user'], status_check_contexts: 'ci' }
  teamFields.forEach(k => r[k] = ['reviewers'])
  setOrganization(r, 'old'); assert.deepEqual(r.push_whitelist_teams, ['reviewers'])
  setOrganization(r, 'new'); teamFields.forEach(k => assert.deepEqual(r[k], []))
  assert.deepEqual(r.push_whitelist_usernames, ['user']); assert.equal(r.status_check_contexts, 'ci')
})
test('空值和零值保留，状态检查按行保存', () => {
  const source = { required_approvals: 0, enable_push: false }; lists.forEach(k => source[k] = [])
  assert.deepEqual(toApiRule(toEditorRule(source, lists)), source)
  assert.deepEqual(toApiRule({ status_check_contexts: ' ci/a \n\nci/b ' }).status_check_contexts, ['ci/a', 'ci/b'])
})

function harness() {
  const state = {}, waiting = [], scheduled = new Map(); let next = 0
  const search = createCandidateSearch({ state, request: (...args) => new Promise((resolve, reject) => waiting.push({ args, resolve, reject })),
    schedule: fn => { scheduled.set(++next, fn); return next }, cancel: id => scheduled.delete(id) })
  search.reset({ serverId: 1, organization: 'a', kind: 'teams' })
  const tick = () => { const values = [...scheduled.values()]; scheduled.clear(); values.forEach(fn => fn()) }
  const flush = () => new Promise(resolve => setImmediate(resolve))
  const result = (name, more = false) => ({ items: [{ name, label: name }], has_more: more })
  return { state, waiting, search, tick, flush, result, scheduled }
}
test('防抖只发送最后一次输入，输入时立即使旧响应失效', async () => {
  const h = harness(); const first = h.search.open()
  h.search.search('旧词'); h.search.search('新词'); assert.equal(h.scheduled.size, 1)
  h.waiting[0].resolve(h.result('旧结果')); await first; assert.deepEqual(h.state.items, [])
  h.tick(); assert.equal(h.waiting[1].args[1], '新词')
  h.waiting[1].resolve(h.result('新结果')); await h.flush(); assert.equal(h.state.items[0].name, '新结果')
})
test('切换组织及实例后旧响应不能混入候选', async () => {
  const h = harness(); const old = h.search.open()
  h.search.reset({ serverId: 2, organization: 'b', kind: 'teams' }); const fresh = h.search.open()
  h.waiting[1].resolve(h.result('新组织')); await fresh
  h.waiting[0].resolve(h.result('旧组织')); await old
  assert.equal(h.state.items[0].name, '新组织'); assert.equal(h.waiting[0].args[3].aborted, true)
})
test('分页防重复加载，失败保留前页，重试使用失败页码', async () => {
  const h = harness(); const first = h.search.open(); h.waiting[0].resolve(h.result('one', true)); await first
  const next = h.search.more(); h.search.more(); assert.equal(h.waiting.length, 2)
  h.waiting[1].reject({ response: { data: { error: '权限不足' } } }); await next
  assert.equal(h.state.items[0].name, 'one'); assert.equal(h.state.error, '权限不足')
  const retry = h.search.retry(); assert.equal(h.waiting[2].args[2], 2)
  h.waiting[2].reject(new Error('连接已断开')); await retry
  assert.equal(h.state.items[0].name, 'one'); assert.match(h.state.error, /已选名单仍然保留/)
  const networkRetry = h.search.retry(); assert.equal(h.waiting[3].args[2], 2)
  h.waiting[3].resolve({ items: [{ name: 'one' }, { name: 'two' }], has_more: false }); await networkRetry
  assert.equal(h.state.items.length, 2); assert.equal(h.state.error, '')
})
test('关闭编辑器会取消定时器和请求，迟到响应不更新状态', async () => {
  const h = harness(); const request = h.search.open(); h.search.search('待发查询'); h.search.dispose()
  assert.equal(h.scheduled.size, 0); h.waiting[0].resolve(h.result('旧结果')); await request
  assert.deepEqual(h.state.items, [])
})
test('未选择组织时不访问候选接口', async () => {
  const h = harness(); h.search.reset(null); await h.search.open(); assert.equal(h.waiting.length, 0)
})
