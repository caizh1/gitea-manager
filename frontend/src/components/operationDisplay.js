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
  return `${current}${total != null ? ' / ' + total : ''} ${{ repos: '个仓库', files: '个文件', tables: '张数据表' }[task.progress_unit] || '项'}`
}

export function backupObservationStale(activity, now) {
  const observed = utcTime(activity.observed_at || activity.action_started_at)
  return !Number.isFinite(observed) || now - observed > 20_000
}

export function backupObservationText(activity, now, disconnected = false, stage = '') {
  if (disconnected || backupObservationStale(activity, now)) return '状态暂无法确认'
  if (activity.state === 'running') {
    if (activity.zip_delta === 0) return '运行中，归档大小暂无变化'
    return stage === 'dump' || activity.zip_bytes != null ? '导出进程运行中' : '命令进程运行中'
  }
  if (activity.state === 'container') {
    const state = { running: '运行中', exited: '已停止', created: '已创建', restarting: '重启中' }[activity.container_state]
    return state ? `最近观测到容器${state}` : '已观测容器状态'
  }
  return activity.state === 'active' ? '最近观测到处理活动' : '等待下一次活动观测'
}
