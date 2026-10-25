// 页面只展示服务端已知事实，不用推送成功、历史预检或估算替代迁移结论。
export const stageList = [
  ['source_check', '校验来源'], ['converting', '历史转换'], ['verifying_history', '内容验证'],
  ['creating', '创建私有仓库'], ['pushing', '推送'], ['verifying_remote', '远端回读'],
]
export const runLabels = { queued: '排队中', running: '执行中', paused: '已暂停', uncertain: '待确认', success: '成功', partial: '部分完成', failed: '失败', cancelled: '已取消', pending: '等待执行' }
export function tone(status) {
  if (['success', 'ready'].includes(status)) return 'success'
  if (['blocked', 'failed'].includes(status)) return 'danger'
  if (['uncertain', 'partial', 'paused'].includes(status)) return 'warning'
  if (['running', 'queued', 'preflight'].includes(status)) return 'primary'
  return 'info'
}
export function checkLabel(check) {
  if (!check) return '尚未预检'
  if (['queued', 'preflight'].includes(check.status)) return check.status === 'queued' ? '预检排队中' : '预检中'
  if (!check.current) return '需重新预检'
  const status = check.repository_status || check.status
  return { ready: '通过', blocked: '受阻', unknown: '未完成检查', failed: '检查失败' }[status] || '未完成检查'
}
export function checkTone(check) {
  return !check || (!check.current && !['queued', 'preflight'].includes(check.status)) ? 'info' : tone(check.repository_status || check.status)
}
export function migrationLabel(repo) {
  if (repo.last_success) return '已迁移'
  if (!repo.migration) return '未迁移'
  return runLabels[repo.migration.status] || '未完成'
}
export function stageIndex(item) {
  if (!item) return -1
  if (item.status === 'success') return stageList.length
  return stageList.findIndex(([key]) => key === item.stage)
}
export function measuredProgress(item) {
  if (item?.stage !== 'verifying_history' || item.status !== 'running') return null
  const match = item.detail?.match(/已逐提交验证\s*(\d+)\s*\/\s*(\d+)/)
  if (!match || Number(match[2]) <= 0 || Number(match[1]) > Number(match[2])) return null
  return { done: Number(match[1]), total: Number(match[2]), percent: Number(match[1]) / Number(match[2]) * 100 }
}
export function dateTime(value) {
  if (!value) return '时间未记录'
  const date = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : value + 'Z')
  return Number.isNaN(date.getTime()) ? '时间未记录' : new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date)
}
