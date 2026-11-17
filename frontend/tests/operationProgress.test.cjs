// 阶段比例不能冒充任务成功，未知总量和 UTC 时间保持真实。
const { test } = require('node:test')
const assert = require('node:assert/strict')
const display = import('../src/components/operationDisplay.js')
test('阶段百分比与任务成功分离', async () => {
  const d = await display, task = { status: 'running', progress_current: 10, progress_total: 10 }
  assert.equal(d.stagePercent(task), 100); assert.equal(d.operationStatus(task), '执行中')
})
test('未知、空总量及非法数量不显示伪百分比', async () => {
  const d = await display
  for (const total of [null, undefined, 0, NaN, Infinity]) assert.equal(d.stagePercent({ progress_current: 0, progress_total: total }), null)
})
test('重新验证时不显示旧成功状态', async () => {
  const d = await display
  assert.equal(d.operationStatus({ status: 'success', verification_status: 'running' }), '执行中')
})
test('计量限制与实际字节显示', async () => {
  const d = await display
  assert.equal(d.stagePercent({ progress_current: 101, progress_total: 100 }), 100)
  assert.equal(d.quantityText({ progress_current: 1048576, progress_total: null, progress_unit: 'bytes' }), '1.0 MB 已传输')
})
test('旧 UTC 时间与带时区时间产生相同耗时', async () => {
  const d = await display
  assert.equal(d.utcTime('2026-09-21T12:00:00'), d.utcTime('2026-09-21T20:00:00+08:00'))
  assert.equal(d.elapsedText('2026-09-21T12:00:00', Date.parse('2026-09-21T12:01:05Z')), '1 分 5 秒')
})

test('区分真实活动、导出暂时无增长与观测中断', async () => {
  const d = await display
  const observed = '2026-09-21T12:00:00Z'
  const now = Date.parse(observed)
  const sample = { observed_at: observed, state: 'running', zip_bytes: 42, zip_delta: 0 }
  assert.equal(d.backupObservationText(sample, now + 10_000), '运行中，归档大小暂无变化')
  assert.equal(d.backupObservationText({ ...sample, zip_delta: 10 }, now), '导出进程运行中')
  assert.equal(d.backupObservationText(sample, now + 21_000), '状态暂无法确认')
  assert.equal(d.backupObservationText(sample, now, true), '状态暂无法确认')
  assert.equal(d.backupObservationText({ action_started_at: observed }, now), '等待下一次活动观测')
  assert.equal(d.backupObservationText({ action_started_at: observed }, now + 21_000), '状态暂无法确认')
  assert.equal(d.backupObservationText({}, now), '状态暂无法确认')
})
