import axios from 'axios'

export const api = axios.create({
  baseURL: '/api',
  withCredentials: true,
})

api.interceptors.response.use(
  res => res,
  err => {
    // 登录失败由表单展示错误，刷新会清空输入并吞掉提示。
    if (err.response && err.response.status === 401 && err.config?.url !== '/login') {
      window.location.reload()
    }
    return Promise.reject(err)
  },
)
