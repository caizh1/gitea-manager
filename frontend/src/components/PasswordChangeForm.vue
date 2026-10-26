<template>
  <el-form class="password-change-form" label-position="top" @submit.prevent="submit">
    <el-form-item v-for="field in fields" :key="field.key" :label="field.label" :for="`${formId}-${field.key}`">
      <el-input
        :id="`${formId}-${field.key}`" v-model="form[field.key]" :name="field.key"
        :type="visible[field.key] ? 'text' : 'password'" :autocomplete="field.autocomplete"
        :placeholder="field.placeholder" :disabled="saving" maxlength="4096" size="large"
        :aria-describedby="field.key === 'new_password' ? `${formId}-rules` : undefined"
      >
        <template #suffix>
          <button type="button" class="password-visibility" :disabled="saving"
            :aria-label="`${visible[field.key] ? '隐藏' : '显示'}${field.label}`"
            :aria-pressed="visible[field.key]" @click="visible[field.key] = !visible[field.key]">
            <component :is="visible[field.key] ? EyeOff : Eye" :size="20" :stroke-width="1.7" aria-hidden="true" />
          </button>
        </template>
      </el-input>
      <p v-if="field.key === 'new_password'" :id="`${formId}-rules`" class="password-rules">至少 12 位，且不能与当前密码相同。</p>
    </el-form-item>
    <el-alert v-if="error" class="password-error" :title="error" type="error" :closable="false" role="alert" />
    <div class="password-session-note" role="note">
      <Info :size="22" :stroke-width="1.7" aria-hidden="true" />
      <span>修改成功后，其他登录会话将退出，当前会话保持登录。</span>
    </div>
    <div class="password-actions">
      <el-button type="primary" native-type="submit" :loading="saving">保存新密码</el-button>
      <el-button :disabled="saving" @click="clear">清空</el-button>
      <slot name="actions" :saving="saving" />
    </div>
  </el-form>
</template>

<script setup>
import { onBeforeUnmount, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Eye, EyeOff, Info } from '@lucide/vue'
import { api, cancelScopeRequests } from '../api'
import { applySession } from '../composables/accessState'

defineProps({ formId: { type: String, default: 'account-password' } })
const fields = [
  { key: 'current_password', label: '当前密码', autocomplete: 'current-password', placeholder: '请输入当前密码' },
  { key: 'new_password', label: '新密码', autocomplete: 'new-password', placeholder: '请输入新密码' },
  { key: 'confirm', label: '确认新密码', autocomplete: 'new-password', placeholder: '请再次输入新密码' },
]
const form = reactive({ current_password: '', new_password: '', confirm: '' })
const visible = reactive({ current_password: false, new_password: false, confirm: false })
const saving = ref(false), error = ref('')
let disposed = false
const requestController = new AbortController()

function clear() {
  for (const field of fields) { form[field.key] = ''; visible[field.key] = false }
  error.value = ''
}

async function submit() {
  if (saving.value) return
  error.value = ''
  if (!form.current_password) error.value = '请输入当前密码'
  else if (Array.from(form.new_password).length < 12) error.value = '新密码至少需要 12 位'
  else if (form.new_password === form.current_password) error.value = '新密码不能与当前密码相同'
  else if (form.new_password !== form.confirm) error.value = '两次新密码不一致'
  if (error.value) return
  saving.value = true
  try {
    const { data } = await api.post('/access/password', {
      current_password: form.current_password, new_password: form.new_password,
    }, { notifyError: false, signal: requestController.signal, timeout: 30000 })
    if (disposed) return
    clear()
    // 改密响应携带新 CSRF；取消旧会话请求，避免在途轮询把旧令牌写回前端。
    cancelScopeRequests()
    applySession(data)
    ElMessage.success('密码已修改，其他登录会话已退出')
  } catch (failure) {
    if (!disposed && failure.code !== 'ERR_CANCELED') {
      error.value = failure.response?.data?.error || '密码修改未确认，请检查连接；如已生效，请使用新密码重新登录'
    }
  } finally {
    if (!disposed) saving.value = false
  }
}

onBeforeUnmount(() => { disposed = true; requestController.abort(); clear() })
</script>

<style scoped>
.password-change-form :deep(.el-form-item) { margin-bottom: 40px; }
.password-change-form :deep(.el-form-item:nth-child(3)) { margin-bottom: 28px; }
.password-change-form :deep(.el-form-item__label) { color: var(--text-primary); font-weight: 600; margin-bottom: 10px; }
.password-change-form :deep(.el-input__wrapper) { min-height: 46px; padding: 0 12px 0 16px; }
.password-change-form :deep(.el-input__suffix-inner) { display: inline-flex; align-items: center; }
.password-visibility { display: inline-flex; align-items: center; justify-content: center; width: 32px; height: 36px; flex: 0 0 32px; border: 0; border-radius: 6px; background: transparent; color: var(--text-muted); cursor: pointer; }
.password-visibility:focus-visible { outline: 2px solid var(--color-primary); outline-offset: 2px; }
.password-visibility:disabled { cursor: not-allowed; opacity: .5; }
.password-rules { margin: 6px 0 0; font-size: 13px; line-height: 1.6; color: var(--text-muted); }
.password-error { margin-bottom: 18px; }
.password-session-note { display: flex; align-items: center; gap: 12px; background: rgba(0,122,255,.055); color: var(--color-primary); padding: 22px 18px; border-radius: 14px; font-size: 14px; line-height: 1.7; }
.password-session-note > svg { flex: 0 0 22px; }
.password-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 22px; }
.password-actions :deep(.el-button) { margin: 0; min-height: 48px; padding: 12px 24px; }
@media (max-width: 600px) { .password-session-note { align-items: flex-start; padding: 14px; } .password-actions :deep(.el-button) { padding: 12px 16px; } }
</style>
