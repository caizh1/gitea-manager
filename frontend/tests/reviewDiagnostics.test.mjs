// Git 源码准备失败在任务详情中保留阶段、原因及退出码。
import test from 'node:test'
import assert from 'node:assert/strict'
import { diagnosticLocation, diagnosticRows, safeDiagnostic } from '../src/components/reviewDiagnostics.js'

test('Git 失败显示源码阶段与退出码', () => {
  const value = { source: 'git_source', phase: 'fetch', reason: 'credential_helper',
    error_code: 'GIT_SOURCE_FAILED', exit_code: 128, error_sha256: 'a'.repeat(64), git_error: 'fatal: Authentication failed' }
  assert.equal(diagnosticLocation(value), 'Git 源码准备')
  assert.equal(safeDiagnostic(value).phase, 'fetch')
  assert.equal(diagnosticRows(value).find(row => row.key === 'exit_code').value, '128')
  assert.equal(diagnosticRows(value).find(row => row.key === 'git_error').value, 'fatal: Authentication failed')
  assert.equal(safeDiagnostic({ ...value, raw_stderr: '私有内容' }).raw_stderr, undefined)
})
