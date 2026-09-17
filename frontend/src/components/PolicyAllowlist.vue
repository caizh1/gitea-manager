<template>
  <div class="policy-allowlist" :class="{ disabled }">
    <label class="native-field"><span>{{ text[usersLabel] }}</span><PolicyIdentitySelect v-model="rule[usersField]" kind="users" :label="text[usersLabel]" :server-id="serverId" :organization="organization" :identities="identities" :disabled="disabled" /></label>
    <label class="native-field"><span>{{ text[teamsLabel] }}</span><PolicyIdentitySelect v-model="rule[teamsField]" kind="teams" :label="text[teamsLabel]" :server-id="serverId" :organization="organization" :identities="identities" :disabled="disabled" /></label>
    <label v-if="keysField" class="native-check"><input v-model="rule[keysField]" type="checkbox" :disabled="disabled"><span>{{ text[keysLabel] }}</span></label>
  </div>
</template>
<script setup>
import PolicyIdentitySelect from './PolicyIdentitySelect.vue'
import text from './policyNativeText.json'
defineProps({ rule: Object, serverId: Number, organization: Object, identities: Object, usersLabel: String, teamsLabel: String, usersField: String, teamsField: String, keysField: String, keysLabel: String, disabled: Boolean })
</script>
<style scoped>
.policy-allowlist { margin: 0 0 16px 26px; min-width: 0; }
.disabled { opacity: .55; }
.native-field { display: flex; flex-direction: column; gap: 7px; margin: 0 0 16px; font-weight: 600; }
.native-check { display: flex; align-items: flex-start; gap: 10px; line-height: 1.5; }
.native-check input { margin: 3px 0 0; flex: 0 0 auto; width: 15px; height: 15px; }
@media (max-width: 600px) { .policy-allowlist { margin-left: 22px; } }
</style>
