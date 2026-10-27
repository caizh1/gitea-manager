// 验证分批失败定位可复制，同时拒绝任意模型文本进入新增诊断字段。
const assert = require('node:assert/strict')
const test = require('node:test')
const loaded = import('../frontend/src/components/reviewDiagnostics.js')

test('分批失败保留具体证据编号与意见序号', async () => {
  const { safeDiagnostic, diagnosticRows, diagnosticText, diagnosticLocation } = await loaded
  const input = { source: 'batch_result', reason: 'batch_evidence_unknown', output_field: 'evidence_ids', finding_index: 2, evidence_id: 'F3H2L91' }
  assert.deepEqual(safeDiagnostic(input), input)
  assert.ok(diagnosticRows(input).some(row => row.key === 'evidence_id' && row.value === 'F3H2L91'))
  assert.equal(JSON.parse(diagnosticText(input)).finding_index, 2)
  assert.equal(diagnosticLocation(input), '分批结果校验')
})

test('新增诊断字段不接受任意文本和非法序号', async () => {
  const { safeDiagnostic, diagnosticText } = await loaded
  const content = '不应复制的模型正文'
  for (const value of [content, 'R1-R9', 'r1', 'R0', {}, 7]) {
    const input = { reason: 'batch_evidence_unknown', output_field: content, finding_index: -1, evidence_id: value }
    assert.deepEqual(safeDiagnostic(input), { reason: 'batch_evidence_unknown' })
    assert.ok(!diagnosticText(input).includes(content))
  }
})

test('合法锚点和原有诊断继续显示', async () => {
  const { safeDiagnostic, diagnosticLocation } = await loaded
  assert.equal(safeDiagnostic({ evidence_id: 'R123' }).evidence_id, 'R123')
  assert.equal(safeDiagnostic({ evidence_id: 'E123456abcd' }).evidence_id, 'E123456abcd')
  assert.equal(safeDiagnostic({ evidence_id: 'Eed719850711' }).evidence_id, 'Eed719850711')
  assert.equal(safeDiagnostic({ finding_index: '2' }), null)
  assert.deepEqual(safeDiagnostic({ source: 'model_transport', http_status: 502 }), { source: 'model_transport', http_status: 502 })
  assert.equal(diagnosticLocation({ source: 'model_transport', phase: 'response_stream' }), '模型响应传输')
})

test('自动检查报告进入可复制诊断且丢弃未知字段', async () => {
  const { safeDiagnostic } = await loaded
  const value = safeDiagnostic({ error_code: 'BATCH_OUTPUT_INVALID', automatic_checks: {
    version: 1, status: 'complete', summary: '固定检查', secret: '不应保留',
    checks: [{ code: 'reference', state: 'confirmed', title: '证据', detail: '编号不在当前单元', raw: '不应保留' }]
  } })
  assert.equal(value.automatic_checks.checks.length, 1)
  assert.ok(!JSON.stringify(value).includes('不应保留'))
})

test('损坏或过大的自动报告保持有界', async () => {
  const { safeAutomaticChecks } = await loaded
  assert.equal(safeAutomaticChecks({ version: 99, status: 'complete', checks: [] }), null)
  const report = safeAutomaticChecks({ version: 1, status: 'partial', summary: '字'.repeat(900),
    checks: Array.from({ length: 30 }, () => ({ code: 'fixed', state: 'unknown', title: '标题', detail: '说明' })) })
  assert.equal(report.checks.length, 12)
  assert.equal(report.summary.length, 500)
})

test('输入超额保留实际计数与当次额度', async () => {
  const { safeDiagnostic, diagnosticRows } = await loaded
  const value = { source: 'local_limit', input_tokens: 321906, input_limit: 65024 }
  assert.deepEqual(safeDiagnostic(value), value)
  assert.ok(JSON.stringify(diagnosticRows(value)).includes('完整输入计数'))
})

test('模型阶段混用显示期望与实际格式', async () => {
  const { safeDiagnostic, diagnosticRows } = await loaded
  const value = { reason: 'source_phase_mismatch', output_field: 'findings', expected_output: 'findings', received_output: 'checks' }
  assert.deepEqual(safeDiagnostic(value), value)
  assert.ok(JSON.stringify(diagnosticRows(value)).includes('当前阶段要求'))
})
