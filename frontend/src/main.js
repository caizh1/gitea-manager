import { createApp } from 'vue'
import ElementPlus, { ElMessage } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
import './styles/global.css'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import App from './App.vue'
import router from './router'

const app = createApp(App)
window.addEventListener('manager-api-error', event => {
  ElMessage({ type: 'error', message: event.detail, showClose: true, duration: 8000, grouping: true, appendTo: document.fullscreenElement || document.body })
})
app.use(ElementPlus, { locale: zhCn })
app.use(router)
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}
app.mount('#app')
