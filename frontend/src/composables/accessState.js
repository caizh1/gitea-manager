import { computed, reactive } from 'vue'

// 只保存当前会话的公开身份；权限判定始终由服务端完成。
export const accessState = reactive({
  ready: false, authenticated: false, user: null, organizations: [], capabilities: [],
  csrfToken: '', sources: [], needsConfig: false, organizationId: null, generation: 0,
})
export const isPlatformAdmin = computed(() => Boolean(accessState.user?.is_platform_admin))
export const currentOrganization = computed(() => accessState.organizations.find(o => String(o.binding_id) === String(accessState.organizationId)) || null)
export const hasCapability = capability => accessState.authenticated && accessState.capabilities.includes(capability)
export const accessReady = computed(() => accessState.authenticated && !accessState.user?.must_change_password && (isPlatformAdmin.value || Boolean(currentOrganization.value)))
export function applySession(data) {
  const previous = JSON.stringify([accessState.user?.id, accessState.user?.is_platform_admin, accessState.capabilities, accessState.organizations])
  Object.assign(accessState, {
    ready: true, authenticated: Boolean(data.authenticated), user: data.user || null,
    organizations: data.organizations || [], capabilities: data.capabilities || [],
    csrfToken: data.csrf_token || '', sources: data.sources || [], needsConfig: Boolean(data.needs_config),
  })
  if (!data.authenticated || isPlatformAdmin.value) accessState.organizationId = null
  else if (!currentOrganization.value) accessState.organizationId = accessState.organizations[0]?.binding_id ?? null
  const next = JSON.stringify([accessState.user?.id, accessState.user?.is_platform_admin, accessState.capabilities, accessState.organizations])
  if (previous !== next) accessState.generation++
}
export function setOrganization(id) {
  if (String(accessState.organizationId) === String(id)) return false
  if (!accessState.organizations.some(o => String(o.binding_id) === String(id))) return false
  accessState.organizationId = id
  accessState.generation++
  return true
}
export function scopedServers(servers) {
  return isPlatformAdmin.value ? servers : servers.filter(s => Number(s.id) === Number(currentOrganization.value?.server_id))
}
