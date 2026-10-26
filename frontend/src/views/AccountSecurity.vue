<template>
  <section class="account-security" aria-labelledby="account-title">
    <header class="account-heading">
      <h1 id="account-title">账号与安全</h1>
      <p>管理当前登录账号的密码与登录安全。</p>
    </header>
    <div class="account-grid">
      <section class="glass-card account-profile" aria-labelledby="profile-title">
        <h2 id="profile-title">当前账号</h2>
        <div class="account-identity">
          <div class="account-avatar" aria-hidden="true">{{ avatar }}</div>
          <div class="account-identity-text"><strong>{{ displayName }}</strong><span>{{ user.username }}</span></div>
        </div>
        <dl class="account-details">
          <div><dt>用户名</dt><dd>{{ user.username }}</dd></div>
          <div><dt>登录方式</dt><dd>{{ isLocal ? '本地账号' : '公司目录账号' }}</dd></div>
          <div><dt>账号角色</dt><dd><el-tag effect="light">{{ role }}</el-tag></dd></div>
        </dl>
        <p class="account-footnote">{{ isLocal ? '此密码用于登录 Gitea Manager。' : '此账号使用公司 LDAP / AD 目录登录。' }}</p>
      </section>
      <section class="glass-card account-password" aria-labelledby="password-title">
        <h2 id="password-title"><LockKeyhole :size="26" :stroke-width="1.7" aria-hidden="true" />修改密码</h2>
        <template v-if="isLocal">
          <p class="account-description">验证当前密码后，为账号设置新密码。</p>
          <PasswordChangeForm />
        </template>
        <div v-else class="directory-password-note" role="note">
          <Info :size="24" :stroke-width="1.7" aria-hidden="true" />
          <div><h3>请在公司目录中修改密码</h3><p>密码由公司 LDAP / AD 统一管理。请使用公司提供的密码修改入口，或联系目录管理员。</p></div>
        </div>
      </section>
    </div>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import { Info, LockKeyhole } from '@lucide/vue'
import PasswordChangeForm from '../components/PasswordChangeForm.vue'
import { accessState } from '../composables/accessState'

const user = computed(() => accessState.user || {})
const isLocal = computed(() => user.value.auth_source === 'local')
const displayName = computed(() => user.value.display_name || user.value.username || '当前账号')
const avatar = computed(() => displayName.value === '平台管理员' ? '管' : displayName.value.slice(0, 1))
const role = computed(() => user.value.is_platform_admin ? '平台管理员' : accessState.organizations.length ? '组织管理员' : '待分配权限')
</script>

<style scoped>
.account-security { max-width: 1200px; margin: 0 0 0 auto; }
.account-heading { margin: 46px 0 38px; }
.account-heading h1 { margin: 0 0 8px; font-size: 36px; font-weight: 750; line-height: 1.3; letter-spacing: -.5px; }
.account-heading p { color: var(--text-secondary); font-size: 16px; line-height: 1.6; }
.account-grid { display: grid; grid-template-columns: minmax(270px, .38fr) minmax(0, .62fr); gap: 24px; align-items: stretch; }
.account-grid > section { min-width: 0; padding: 32px 32px 40px; }
.account-grid h2 { display: flex; align-items: center; gap: 14px; margin: 0; font-size: 22px; font-weight: 700; line-height: 1.5; }
.account-grid h2 > svg { flex: 0 0 26px; }
.account-profile { display: flex; flex-direction: column; }
.account-identity { display: flex; align-items: center; gap: 22px; padding: 28px 0; border-bottom: 1px solid rgba(15,23,42,.1); }
.account-avatar { display: flex; align-items: center; justify-content: center; flex: 0 0 80px; width: 80px; height: 80px; background: rgba(0,122,255,.09); color: var(--color-primary); border-radius: 50%; font-size: 32px; font-weight: 600; }
.account-identity-text { display: flex; flex-direction: column; gap: 5px; min-width: 0; overflow-wrap: anywhere; }
.account-identity-text strong { font-size: 21px; line-height: 1.5; }
.account-identity-text span { font-size: 17px; color: var(--text-secondary); }
.account-details { display: grid; gap: 26px; padding: 30px 0; }
.account-details > div { display: grid; grid-template-columns: 100px minmax(0, 1fr); gap: 14px; align-items: center; }
.account-details dt { color: var(--text-secondary); }
.account-details dd { margin: 0; overflow-wrap: anywhere; }
.account-details :deep(.el-tag) { font-size: 14px; max-width: 100%; white-space: normal; height: auto; padding: 7px 10px; }
.account-footnote { margin-top: auto; padding-top: 32px; font-size: 13px; line-height: 1.7; color: var(--text-muted); }
.account-description { margin: 14px 0 28px; color: var(--text-secondary); font-size: 14px; line-height: 1.7; }
.directory-password-note { display: flex; align-items: flex-start; gap: 14px; padding: 22px; background: rgba(0,122,255,.05); border-radius: 14px; margin-top: 28px; }
.directory-password-note > svg { flex: 0 0 24px; color: var(--color-primary); }
.directory-password-note h3 { font-size: 16px; margin: 0 0 10px; }
.directory-password-note p { color: var(--text-secondary); line-height: 1.8; }
@media (max-width: 1100px) { .account-grid { grid-template-columns: minmax(250px, .38fr) minmax(0, .62fr); gap: 18px; } .account-grid > section { padding: 24px; } .account-avatar { flex-basis: 64px; width: 64px; height: 64px; } .account-identity { gap: 14px; } }
@media (max-width: 800px) { .account-grid { grid-template-columns: minmax(0, 1fr); } .account-heading { margin-top: 16px; margin-bottom: 24px; } .account-heading h1 { font-size: 26px; } .account-heading p { font-size: 14px; } .account-footnote { padding-top: 4px; } .account-details { gap: 18px; padding: 22px 0; } }
@media (max-width: 450px) { .account-grid > section { padding: 20px; } .account-grid h2 { font-size: 20px; } .account-details > div { grid-template-columns: 80px minmax(0,1fr); } }
</style>
