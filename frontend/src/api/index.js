import axios from 'axios'
import { accessState } from '../composables/accessState.js'

export const api = axios.create({ baseURL: '/api', withCredentials: true })
let scopeController = new AbortController()
export function cancelScopeRequests() {
  scopeController.abort()
  scopeController = new AbortController()
}
api.interceptors.request.use(config => {
  config.headers = config.headers || {}
  if (accessState.csrfToken) config.headers['X-CSRF-Token'] = accessState.csrfToken
  if (accessState.organizationId != null) config.headers['X-Organization-ID'] = String(accessState.organizationId)
  config.accessGeneration = accessState.generation
  const controller = new AbortController(), signals = [scopeController.signal, config.signal].filter(Boolean)
  const abort = () => controller.abort()
  for (const signal of signals) {
    if (signal.aborted) controller.abort()
    else signal.addEventListener('abort', abort, { once: true })
  }
  config.releaseAccessSignals = () => signals.forEach(signal => signal.removeEventListener('abort', abort))
  config.signal = controller.signal
  return config
}, undefined, { synchronous: true })
api.interceptors.response.use(
  response => {
    response.config.releaseAccessSignals?.()
    if (response.config.accessGeneration !== accessState.generation) throw new axios.CanceledError('权限范围已切换')
    return response
  },
  error => {
    error.config?.releaseAccessSignals?.()
    if (error.config && error.config.accessGeneration !== accessState.generation) return Promise.reject(new axios.CanceledError('权限范围已切换'))
    // 测试账号失败只显示在测试表单；真实会话失效卸载页面并清空数据。
    if (error.response?.status === 401 && !['/login', '/session', '/access/directory/test-login'].includes(error.config?.url)) {
      window.dispatchEvent(new Event('manager-session-expired'))
    }
    if (accessState.authenticated && [403, 404, 409].includes(error.response?.status) && error.config?.url !== '/session') window.dispatchEvent(new Event('manager-access-changed'))
    return Promise.reject(error)
  },
)
