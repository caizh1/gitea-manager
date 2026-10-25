const notified = new WeakSet()

export function apiErrorMessage(error, fallback = '请求未完成，请刷新核对操作结果') {
  const data = error?.response?.data
  const messages = [data?.error, data?.detail].filter(value => typeof value === 'string' && value.trim())
  if (messages.length) return [...new Set(messages)].join('：')
  if (typeof data?.message === 'string' && data.message.trim()) return data.message
  if (['ECONNABORTED', 'ETIMEDOUT'].includes(error?.code)) return '请求超时，请刷新核对操作结果'
  const status = error?.response?.status
  if (status) return `请求失败（HTTP ${status}），请刷新核对操作结果`
  if (error?.request || error?.code === 'ERR_NETWORK') return '网络连接失败，请检查连接并刷新核对操作结果'
  return error?.message || fallback
}

// 请求层和页面可以共同调用；同一个错误只弹出一次，取消和旧权限范围不提示。
export function notifyApiError(error, fallback) {
  if (!error || typeof error !== 'object' || error.code === 'ERR_CANCELED' || notified.has(error)) return
  if (typeof window === 'undefined') return
  notified.add(error)
  window.dispatchEvent(new CustomEvent('manager-api-error', { detail: apiErrorMessage(error, fallback) }))
}

export async function decodeApiError(error) {
  const data = error?.response?.data
  // 下载接口失败也可能返回 JSON；成功的二进制响应保持原样。
  if (typeof Blob !== 'undefined' && data instanceof Blob && data.size <= 1024 * 1024) {
    try { error.response.data = JSON.parse(await data.text()) } catch { /* 网关 HTML 或非 JSON 响应使用状态码提示。 */ }
  }
}
