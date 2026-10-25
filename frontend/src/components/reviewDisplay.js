// 旧任务和缺字段结果统一在显示边界兼容；不修改原始审查证据。
export { apiErrorMessage as errorMessage } from '../api/errors.js'
export const statusType = s => s === 'succeeded' ? 'success' : s === 'failed' ? 'danger' : ['publish_unknown', 'superseded', 'cancelling'].includes(s) ? 'warning' : s === 'cancelled' ? 'info' : 'primary'
export function coverageGroups(coverage) {
  const groups = new Map()
  for (const item of Array.isArray(coverage?.skipped) ? coverage.skipped : []) {
    const path = item.path || '未定位文件'
    if (!groups.has(path)) groups.set(path, { path, reasons: new Map() })
    const reason = item.reason || '未提供原因'
    const own = groups.get(path).reasons
    if (!own.has(reason)) own.set(reason, { reason, count: 0, scopes: new Set() })
    const row = own.get(reason); row.count++
    if (item.scope) row.scopes.add(item.scope)
  }
  return [...groups.values()].map(g => ({ path: g.path, reasons: [...g.reasons.values()].map(r => ({ ...r, scopes: [...r.scopes] })) }))
}
