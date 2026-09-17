// 表单只转换展示方式，API 字段与有效规则语义保持不变。
export const teamFields = ['push_whitelist_teams', 'force_push_allowlist_teams', 'approvals_whitelist_teams', 'merge_whitelist_teams']
export const pushMode = rule => !rule.enable_push ? 'none' : rule.enable_push_whitelist ? 'list' : 'all'
export const forceMode = rule => !rule.enable_force_push ? 'none' : rule.enable_force_push_allowlist ? 'list' : 'all'
export function setPushMode(rule, mode) {
  rule.enable_push = mode !== 'none'
  rule.enable_push_whitelist = mode === 'list'
}
export function setForceMode(rule, mode) {
  rule.enable_force_push = mode !== 'none'
  rule.enable_force_push_allowlist = mode === 'list'
}
export function setOrganization(rule, organization) {
  if (rule.team_org === organization) return
  teamFields.forEach(field => { rule[field] = [] })
  rule.team_org = organization
}
export function toEditorRule(rule, listFields) {
  const copy = { ...rule }
  listFields.forEach(field => { copy[field] = field === 'status_check_contexts' ? (rule[field] || []).join('\n') : [...(rule[field] || [])] })
  return copy
}
export function toApiRule(rule) {
  return { ...rule, status_check_contexts: rule.status_check_contexts.split('\n').map(s => s.trim()).filter(Boolean) }
}

// 查询序号在输入时失效，而非请求发出时失效；旧响应不能覆盖新关键词。
export function createCandidateSearch({ request, state, schedule = setTimeout, cancel = clearTimeout }) {
  let generation = 0, timer = null, controller = null, disposed = false, context = null
  function invalidate() {
    ++generation
    if (timer !== null) cancel(timer)
    timer = null
    controller?.abort()
    controller = null
  }
  function reset(nextContext) {
    invalidate(); context = nextContext
    Object.assign(state, { items: [], query: '', page: 0, hasMore: false, loading: false, error: '', loaded: false })
  }
  async function load(page) {
    if (disposed || !context) return
    const sequence = generation, captured = { ...context }, query = state.query
    controller = new AbortController()
    state.loading = true; state.error = ''
    try {
      const data = await request(captured, query, page, controller.signal)
      if (disposed || sequence !== generation) return
      const merged = new Map((page === 1 ? [] : state.items).map(item => [item.name, item]))
      data.items.forEach(item => merged.set(item.name, item))
      Object.assign(state, { items: [...merged.values()], page, hasMore: data.has_more, loaded: true })
    } catch (error) {
      if (disposed || sequence !== generation) return
      state.error = error.response?.data?.error || '候选查询失败，请重试；已选名单仍然保留。'
    } finally {
      if (!disposed && sequence === generation) { state.loading = false; controller = null }
    }
  }
  function search(query) {
    invalidate()
    Object.assign(state, { query: query.trim(), items: [], page: 0, hasMore: false, error: '', loaded: false, loading: true })
    timer = schedule(() => { timer = null; load(1) }, 500)
  }
  return {
    reset, search,
    open() { if (!state.loaded && !state.loading) return load(1) },
    more() { if (state.hasMore && !state.loading) return load(state.page + 1) },
    retry() { if (!state.loading) return load(state.page ? state.page + 1 : 1) },
    dispose() { disposed = true; invalidate() },
  }
}
