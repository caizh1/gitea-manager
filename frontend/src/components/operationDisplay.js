export const operationActive = task => task?.status === 'running' || task?.verification_status === 'running' || task?.last_status === 'running'
export const operationStatus = task => operationActive(task) ? '执行中' : ({ success: '已完成', failed: '失败', pending: '待执行' }[task?.status || task?.last_status] || '状态待确认')
export function stagePercent(task) {
  const total = Number(task?.progress_total), current = Number(task?.progress_current)
  return task?.progress_total != null && task?.progress_current != null && total > 0 && Number.isFinite(total) && Number.isFinite(current) ? Math.min(100, Math.max(0, Math.floor(current / total * 100))) : null
}
export function utcTime(value) {
  if (!value) return null
  return Date.parse(/[Zz]|[+-]\d\d:\d\d$/.test(value) ? value : value + 'Z')
}
export function elapsedText(start, end) {
  const begin = utcTime(start)
  if (!Number.isFinite(begin)) return '—'
  const seconds = Math.max(0, Math.floor((end - begin) / 1000))
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}
export function quantityText(task) {
  const current = task?.progress_current, total = task?.progress_total
  if (current == null) return ''
  const bytes = value => value >= 1073741824 ? `${(value / 1073741824).toFixed(2)} GB` : value >= 1048576 ? `${(value / 1048576).toFixed(1)} MB` : `${value} B`
  if (task.progress_unit === 'bytes') return `${bytes(current)}${total != null ? ' / ' + bytes(total) : ' 已传输'}`
  return `${current}${total != null ? ' / ' + total : ''} ${{ repos: '仓库', files: '文件' }[task.progress_unit] || '项'}`
}
