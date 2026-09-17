<template>
  <div class="policy-rule-editor">
    <section :aria-label="text.protect_patterns">
      <h5>{{ text.protect_patterns }}</h5>
      <label v-for="field in patterns" :key="field.key" class="native-field">
        <span>{{ text[field.label] }}<span v-if="field.key === 'rule_name'" class="required-mark"> *</span></span>
        <input v-model="rule[field.key]" :aria-label="text[field.label]" :required="field.key === 'rule_name'" type="text">
        <p class="help" v-html="help(field.label + '_desc')" />
      </label>
    </section>
    <section :aria-label="text.event_push">
      <h5>{{ text.event_push }}</h5>
      <div v-for="option in pushOptions" :key="option.value" class="option-field">
        <label class="native-check"><input type="radio" :name="'push-' + uid" :value="option.value" :checked="pushMode(rule) === option.value" @change="setPushMode(rule, option.value)"><span>{{ text[option.label] }}</span></label>
        <p class="help indent">{{ text[option.label + '_desc'] }}</p>
      </div>
      <PolicyAllowlist v-bind="context" users-label="protect_whitelist_users" teams-label="protect_whitelist_teams" users-field="push_whitelist_usernames" teams-field="push_whitelist_teams" keys-field="push_whitelist_deploy_keys" keys-label="protect_whitelist_deploy_keys" :disabled="pushMode(rule) !== 'list'" />
      <div class="option-field"><label class="native-check"><input v-model="rule.require_signed_commits" type="checkbox"><span>{{ text.require_signed_commits }}</span></label><p class="help indent">{{ text.require_signed_commits_desc }}</p></div>
    </section>
    <section :aria-label="text.event_force_push">
      <h5>{{ text.event_force_push }}</h5>
      <div v-for="option in forceOptions" :key="option.value" class="option-field">
        <label class="native-check"><input type="radio" :name="'force-' + uid" :value="option.value" :checked="forceMode(rule) === option.value" @change="setForceMode(rule, option.value)"><span>{{ text[option.label] }}</span></label>
        <p class="help indent">{{ text[option.label + '_desc'] }}</p>
      </div>
      <PolicyAllowlist v-bind="context" users-label="protect_force_push_allowlist_users" teams-label="protect_force_push_allowlist_teams" users-field="force_push_allowlist_usernames" teams-field="force_push_allowlist_teams" keys-field="force_push_allowlist_deploy_keys" keys-label="protect_force_push_allowlist_deploy_keys" :disabled="forceMode(rule) !== 'list'" />
    </section>
    <section :aria-label="text.event_pull_request_approvals">
      <h5>{{ text.event_pull_request_approvals }}</h5>
      <label class="native-field"><span>{{ text.protect_required_approvals }}</span><input v-model.number="rule.required_approvals" type="number" min="0" max="100" :aria-label="text.protect_required_approvals"><p class="help">{{ text.protect_required_approvals_desc }}</p></label>
      <div class="option-field"><label class="native-check"><input v-model="rule.enable_approvals_whitelist" type="checkbox"><span>{{ text.protect_approvals_whitelist_enabled }}</span></label><p class="help indent">{{ text.protect_approvals_whitelist_enabled_desc }}</p></div>
      <PolicyAllowlist v-bind="context" users-label="protect_approvals_whitelist_users" teams-label="protect_approvals_whitelist_teams" users-field="approvals_whitelist_username" teams-field="approvals_whitelist_teams" :disabled="!rule.enable_approvals_whitelist" />
      <div v-for="field in staleFields" :key="field" class="option-field" :class="{ disabled: field === 'ignore_stale_approvals' && rule.dismiss_stale_approvals }">
        <label class="native-check"><input v-model="rule[field]" type="checkbox" :disabled="field === 'ignore_stale_approvals' && rule.dismiss_stale_approvals"><span>{{ text[field] }}</span></label><p class="help indent">{{ text[field + '_desc'] }}</p>
      </div>
      <div class="option-field"><label class="native-check"><input v-model="rule.enable_status_check" type="checkbox"><span>{{ text.protect_check_status_contexts }}</span></label><p class="help indent">{{ text.protect_check_status_contexts_desc }}</p></div>
      <div class="indent" :class="{ disabled: !rule.enable_status_check }"><label class="native-field"><span>{{ text.protect_status_check_patterns }}</span><textarea v-model="rule.status_check_contexts" :aria-label="text.protect_status_check_patterns" rows="3" :disabled="!rule.enable_status_check" /><p class="help">{{ text.protect_status_check_patterns_desc }}</p></label></div>
    </section>
    <section :aria-label="text.event_pull_request_merge">
      <h5>{{ text.event_pull_request_merge }}</h5>
      <div v-for="option in mergeOptions" :key="String(option.value)" class="option-field">
        <label class="native-check"><input v-model="rule.enable_merge_whitelist" type="radio" :name="'merge-' + uid" :value="option.value"><span>{{ text[option.label] }}</span></label><p class="help indent">{{ text[option.label + '_desc'] }}</p>
      </div>
      <PolicyAllowlist v-bind="context" users-label="protect_merge_whitelist_users" teams-label="protect_merge_whitelist_teams" users-field="merge_whitelist_usernames" teams-field="merge_whitelist_teams" :disabled="!rule.enable_merge_whitelist" />
      <div v-for="field in mergeFields" :key="field.key" class="option-field"><label class="native-check"><input v-model="rule[field.key]" type="checkbox"><span>{{ text[field.label] }}</span></label><p class="help indent">{{ text[field.label + '_desc'] }}</p></div>
    </section>
  </div>
</template>
<script setup>
import { computed, getCurrentInstance } from 'vue'
import PolicyAllowlist from './PolicyAllowlist.vue'
import text from './policyNativeText.json'
import { forceMode, pushMode, setForceMode, setPushMode } from './policyForm'
const props = defineProps({ rule: Object, serverId: Number, organization: Object, identities: Object })
const uid = getCurrentInstance().uid
const context = computed(() => ({ rule: props.rule, serverId: props.serverId, organization: props.organization, identities: props.identities }))
const patterns = [{ key: 'rule_name', label: 'protect_branch_name_pattern' }, { key: 'protected_file_patterns', label: 'protect_protected_file_patterns' }, { key: 'unprotected_file_patterns', label: 'protect_unprotected_file_patterns' }]
const pushOptions = [{ value: 'none', label: 'protect_disable_push' }, { value: 'all', label: 'protect_enable_push' }, { value: 'list', label: 'protect_whitelist_committers' }]
const forceOptions = [{ value: 'none', label: 'protect_disable_force_push' }, { value: 'all', label: 'protect_enable_force_push_all' }, { value: 'list', label: 'protect_enable_force_push_allowlist' }]
const mergeOptions = [{ value: false, label: 'protect_enable_merge' }, { value: true, label: 'protect_merge_whitelist_committers' }]
const staleFields = ['dismiss_stale_approvals', 'ignore_stale_approvals']
const mergeFields = [{ key: 'block_on_rejected_reviews', label: 'block_rejected_reviews' }, { key: 'block_on_official_review_requests', label: 'block_on_official_review_requests' }, { key: 'block_on_outdated_branch', label: 'block_outdated_branch' }, { key: 'block_admin_merge_override', label: 'block_admin_merge_override' }]
// 仅渲染随源码固定的原生文案；不把用户输入插入 HTML。
function help(key) { return text[key].replace('%s', 'https://github.com/gobwas/glob').replace('%[1]s', 'https://pkg.go.dev/github.com/gobwas/glob#Compile').replace('%[2]s', 'github.com/gobwas/glob') }
</script>
<style scoped>
.policy-rule-editor { min-width: 0; color: #212121; font-size: 14px; line-height: 1.5; }
section { min-width: 0; margin-bottom: 24px; }
h5 { font-size: 14px; line-height: 1.4; padding-bottom: 8px; border-bottom: 1px solid #d4d4d5; margin: 24px 0 16px; font-weight: 600; }
.native-field { display: flex; flex-direction: column; gap: 7px; margin-bottom: 16px; font-weight: 600; }
.native-field input, .native-field textarea { border: 1px solid #d4d4d5; border-radius: 4px; padding: 9px 12px; width: 100%; min-width: 0; box-sizing: border-box; background: #fff; color: inherit; font: inherit; font-weight: 400; }
.native-field textarea { resize: vertical; }
.native-field input:focus, .native-field textarea:focus { border-color: #85b7d9; outline: 2px solid #85b7d933; }
.native-check { display: flex; align-items: flex-start; gap: 10px; cursor: pointer; }
.native-check input { margin: 3px 0 0; flex: 0 0 auto; width: 15px; height: 15px; accent-color: #4183c4; }
.option-field { margin: 0 0 16px; }
.help { color: #707070; font-size: 13px; line-height: 1.6; margin: 0; font-weight: 400; overflow-wrap: anywhere; }
.option-field .help { margin-top: 4px; }
.help :deep(a) { color: #4183c4; }
.indent { margin-left: 26px; }
.disabled { opacity: .55; }
.required-mark { color: #db2828; }
@media (max-width: 600px) { .indent { margin-left: 22px; } }
</style>
