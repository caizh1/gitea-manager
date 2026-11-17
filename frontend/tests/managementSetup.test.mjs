import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import { createRenderer, nextTick } from 'vue'
import { parse, compileScript } from '@vue/compiler-sfc'
import * as vue from 'vue'

const filename = new URL('../src/views/ManagementSetup.vue', import.meta.url)
const { descriptor } = parse(fs.readFileSync(filename, 'utf8'))
const compiled = compileScript(descriptor, { id: 'management-setup-test' }).content
  .replace(/^import .* from .*\n/gm, '')
  .replace('export default', 'const component =')
const renderer = createRenderer({ createComment: () => ({}), insert() {}, remove() {}, parentNode() {}, nextSibling() {} })

async function mountedSetup(instances = []) {
  const calls = []
  const api = {
    async get(url) {
      if (url.endsWith('/status')) return { data: { runtime: { ready: true }, csrf: '隔离页面校验' } }
      if (url.endsWith('/ssh-public-key')) return { data: { public_key: '隔离测试公钥' } }
      return { data: { reports: [] } }
    },
    async post(url, body) {
      calls.push({ url, body })
      return { data: url.endsWith('/discover') ? { instances } : { ok: true, id: 1 } }
    },
  }
  const context = vm.createContext({ ...vue, api, ElMessage: { warning() {} }, ElMessageBox: {},
    ArchiveRestore: {}, CircleCheck: {}, DatabaseBackup: {}, Info: {}, Search: {}, Server: {}, ShieldCheck: {},
    setInterval: () => 1, clearInterval() {} })
  const component = vm.runInContext(compiled + '\ncomponent', context)
  let state
  const app = renderer.createApp({ setup() { state = component.setup({}, { expose() {} }); return () => null } })
  app.mount({})
  await new Promise(resolve => setImmediate(resolve))
  await nextTick()
  return { app, state, calls }
}

const first = { id: 'first', name: '实例一', project: '项目一', running: true }
const second = { id: 'second', name: '实例二', project: '项目二', running: true }

test('唯一运行实例自动选中，角色保持显式选择', async () => {
  const { app, state } = await mountedSetup([first])
  try {
    assert.equal(state.selected.value, first.id)
    assert.match(state.discoveryHint.value, /自动选中/)
    assert.equal(state.canConnect.value, false)
    state.form.value.role = 'primary'
    assert.equal(state.canConnect.value, true)
  } finally { app.unmount() }
})

test('多个实例等待选择，停止实例不可接入', async () => {
  const { app, state } = await mountedSetup([first, second])
  try {
    assert.equal(state.selected.value, '')
    assert.match(state.discoveryHint.value, /点击下方卡片选择/)
    state.form.value.role = 'primary'
    state.selected.value = second.id
    assert.equal(state.canConnect.value, true)
    state.instances.value = [{ ...second, running: false }]
    assert.equal(state.canConnect.value, false)
  } finally { app.unmount() }
})

test('远程发现不携带令牌，接入提交原有令牌并在成功后清空', async () => {
  const { app, state, calls } = await mountedSetup([first])
  try {
    state.form.value.mode = 'ssh'
    state.form.value.host = 'gitea.example.test'
    await nextTick()
    state.form.value.api_token = ' 已有管理员令牌 '
    state.form.value.role = 'backup'
    await state.discover()
    const discovery = calls.filter(call => call.url.endsWith('/discover')).at(-1)
    assert.equal('api_token' in discovery.body, false)
    assert.equal(state.canConnect.value, true)
    await state.connect()
    const connection = calls.find(call => call.url.endsWith('/connect'))
    assert.equal(connection.body.api_token, '已有管理员令牌')
    assert.equal(connection.body.instance_id, first.id)
    assert.equal(state.form.value.api_token, '')
    assert.equal(state.connected.value, true)
  } finally { app.unmount() }
})

test('改变目标主机清空旧选择、旧令牌和指纹状态', async () => {
  const { app, state } = await mountedSetup([first])
  try {
    state.trustedHost.value = true
    state.form.value.api_token = '已有管理员令牌'
    state.form.value.host = 'another.example.test'
    await nextTick()
    assert.equal(state.selected.value, '')
    assert.equal(state.instances.value.length, 0)
    assert.equal(state.form.value.api_token, '')
    assert.equal(state.trustedHost.value, false)
  } finally { app.unmount() }
})
