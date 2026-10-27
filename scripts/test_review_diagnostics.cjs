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
  assert.equal(safeDiagnostic({ finding_index: '2' }), null)
  assert.deepEqual(safeDiagnostic({ source: 'model_transport', http_status: 502 }), { source: 'model_transport', http_status: 502 })
  assert.equal(diagnosticLocation({ source: 'model_transport', phase: 'response_stream' }), '模型响应传输')
})
