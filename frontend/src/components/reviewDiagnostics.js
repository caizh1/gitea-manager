const scalarFields = ['thinking_protocol', 'thinking_enabled', 'thinking_token_budget', 'reasoning_chars', 'content_chars', 'task_remaining_seconds', 
  'diagnostic_version', 'status', 'error_code', 'message', 'source', 'stage', 'phase', 'branch', 'cause_stage',
  'rejection_branch', 'category', 'job', 'job_id', 'unit', 'model_phase', 'session_id', 'call_id', 'request_id', 'time_utc',
  'native_session_id', 'exception_type', 'reason', 'expected_output', 'received_output', 'extraction_path', 'parser_reason', 'parser_message',
  'native_error_type', 'done_seen', 'completion_complete',
  'json_line', 'json_column', 'json_offset', 'response_bytes', 'received_bytes', 'response_chars',
  'text_part_count', 'part_count', 'parts_count', 'result_type', 'limit_bytes', 'response_limit_bytes', 'reserved_bytes',
  'input_bytes', 'input_limit_bytes', 'input_tokens', 'input_limit', 'body_limit_bytes', 'model_call', 'transfer_phase',
  'request_started_at', 'elapsed_seconds', 'timeout_seconds', 'headers_elapsed_seconds',
  'first_data_elapsed_seconds', 'last_data_elapsed_seconds', 'exit_code', 'finish', 'finish_reason',
  'model_context_tokens', 'upstream_output_tokens', 'errno', 'event_type', 'error_type',
  'tls_errno', 'tls_exception_type', 'tls_phase', 'tls_verify_code',
  'http_status', 'upstream_http_status', 'upstream_error_type', 'upstream_error_code',
  'upstream_request_id', 'upstream_trace_id', 'error_bytes', 'declared_error_bytes', 'error_sha256', 'error_truncated',
  'captured_bytes', 'captured_sha256', 'secondary_exception_type',
  'executor_error_code', 'secondary_http_status'
]

const labels = {
  thinking_protocol: '思考控制协议', thinking_enabled: '请求启用思考', thinking_token_budget: '配置思考额度', reasoning_chars: '已接收思考字符数', content_chars: '已接收正文字数', task_remaining_seconds: '模型会话剩余秒数',
  error_code: '错误代码', source: '错误来源', stage: '失败层级', phase: '失败阶段',
  branch: '具体分支', cause_stage: '原因阶段', rejection_branch: '拒绝分支', category: '错误类别',
  session_id: '会话标识', call_id: '调用标识', request_id: '请求标识', time_utc: '发生时间', exception_type: '异常类型',
  native_session_id: '原生会话标识', reason: '失败原因', extraction_path: '结果提取路径', parser_reason: '解析错误类型', parser_message: '解析说明',
  output_field: '失败字段', finding_index: '意见序号', evidence_id: '无法核对的证据编号',
  input_tokens: '完整输入计数', input_limit: '当次可用额度',
  expected_output: '当前阶段要求', received_output: '模型实际返回',
  native_error_type: '原生错误类型', done_seen: '结束标记已收到', completion_complete: '响应完整',
  json_line: '解析文本行', json_column: '解析文本列', json_offset: '解析文本偏移', response_bytes: '响应长度',
  received_bytes: '已接收长度', response_chars: '响应字符数', text_part_count: '文本分段数', part_count: '响应分段数', parts_count: '响应分段数',
  result_type: '结果类型', limit_bytes: '长度上限', response_limit_bytes: '响应上限', reserved_bytes: '保留空间', input_bytes: '输入长度', input_limit_bytes: '输入上限', body_limit_bytes: '正文上限',
  model_call: '模型调用序号', transfer_phase: '传输阶段', request_started_at: '请求开始时间', elapsed_seconds: '已耗时', timeout_seconds: '超时上限',
  headers_elapsed_seconds: '响应头耗时', first_data_elapsed_seconds: '首段数据耗时', last_data_elapsed_seconds: '末段数据耗时',
  exit_code: '退出码', finish: '原生结束原因', finish_reason: '结束原因', http_status: 'HTTP 状态',
  model_context_tokens: '模型上下文额度', upstream_output_tokens: '上游输出额度', errno: '系统错误码', event_type: '事件类型', error_type: '错误类型',
  tls_errno: 'TLS 系统错误码', tls_exception_type: 'TLS 异常类型', tls_phase: 'TLS 阶段', tls_verify_code: 'TLS 校验码',
  upstream_http_status: '上游 HTTP 状态', upstream_error_type: '上游错误类型', upstream_error_code: '上游错误代码',
  upstream_request_id: '上游请求标识', upstream_trace_id: '上游追踪标识', error_bytes: '错误证据长度', error_sha256: '错误证据摘要', error_truncated: '错误证据已截断',
  declared_error_bytes: '上游声明错误长度', captured_bytes: '受限证据长度', captured_sha256: '受限证据摘要', secondary_exception_type: '次级传输异常',
  job: '任务标识', job_id: '任务标识', unit: '审查单元', model_phase: '模型阶段', source: '错误来源', phase: '执行阶段', stage: '失败层级'
}

const hasValue = value => value !== undefined && value !== null && value !== ''
export function safeAutomaticChecks(value) {
  if (!value || value.version !== 1 || !['complete', 'partial', 'unavailable', 'unsupported'].includes(value.status) || !Array.isArray(value.checks)) return null
  const text = item => typeof item === 'string' ? item.slice(0, 500) : ''
  return { version: 1, status: value.status, summary: text(value.summary),
    checks: value.checks.slice(0, 12).filter(row => row && ['confirmed', 'clear', 'unknown'].includes(row.state))
      .map(row => ({ code: text(row.code), state: row.state, title: text(row.title), detail: text(row.detail) })) }
}
const secondaryFields = new Set(['error_code', 'source', 'phase', 'reason', 'exception_type', 'native_error_type', 'status', 'call_id', 'session_id'])

function safeSecondary(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const safe = {}
  for (const key of secondaryFields) if (hasValue(value[key]) && ['string', 'number', 'boolean'].includes(typeof value[key])) safe[key] = value[key]
  return Object.keys(safe).length ? safe : null
}

export function safeDiagnostic(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const safe = {}
  for (const key of scalarFields) if (hasValue(value[key]) && ['string', 'number', 'boolean'].includes(typeof value[key])) safe[key] = value[key]
  const checks = safeAutomaticChecks(value.automatic_checks)
  if (checks) safe.automatic_checks = checks
  // 只展示后端允许的定位元数据，不把任意模型文本当成证据编号复制出来。
  if (['evidence_ids', 'findings', 'checks', 'summary', 'limitations', 'line'].includes(value.output_field)) safe.output_field = value.output_field
  if (Number.isSafeInteger(value.finding_index) && value.finding_index > 0) safe.finding_index = value.finding_index
  if (typeof value.evidence_id === 'string' && value.evidence_id.length <= 180 && /^(?:E[0-9a-f]{1,16}|R[1-9][0-9]*|F[1-9][0-9]*H[1-9][0-9]*L[1-9][0-9]*)$/.test(value.evidence_id)) safe.evidence_id = value.evidence_id
  for (const key of ['finish_reasons', 'completion_warnings']) if (Array.isArray(value[key])) safe[key] = value[key].filter(item => ['string', 'number', 'boolean'].includes(typeof item)).slice(0, 20)
  if (Array.isArray(value.frames)) safe.frames = value.frames.slice(0, 6).map(frame => ({
    ...(hasValue(frame?.file) ? { file: String(frame.file) } : {}),
    ...(hasValue(frame?.line) ? { line: frame.line } : {}),
    ...(hasValue(frame?.function) ? { function: String(frame.function) } : {})
  })).filter(frame => Object.keys(frame).length)
  if (value.stack_location && typeof value.stack_location === 'object') {
    const location = value.stack_location
    safe.stack_location = {
      ...(hasValue(location.file) ? { file: String(location.file) } : {}),
      ...(hasValue(location.line) ? { line: location.line } : {}),
      ...(hasValue(location.function) ? { function: String(location.function) } : {})
    }
    if (!Object.keys(safe.stack_location).length) delete safe.stack_location
  }
  if (Array.isArray(value.secondary)) safe.secondary = value.secondary.map(safeSecondary).filter(Boolean)
  else if (value.secondary && typeof value.secondary === 'object') {
    const secondary = safeSecondary(value.secondary)
    if (secondary) safe.secondary = [secondary]
  }
  return Object.keys(safe).length ? safe : null
}

export function diagnosticRows(value) {
  const safe = safeDiagnostic(value)
  if (!safe) return []
  return Object.entries(safe)
    .filter(([key]) => labels[key])
    .map(([key, value]) => ({ key, label: labels[key], value: /(?:bytes|limit_bytes|reserved_bytes)$/.test(key) ? `${value} 字节` : /elapsed_seconds$|timeout_seconds$/.test(key) ? `${value} 秒` : String(value) }))
}

export const diagnosticText = value => JSON.stringify(safeDiagnostic(value) || {}, null, 2)

export function diagnosticLocation(value) {
  const safe = safeDiagnostic(value)
  if (!safe) return ''
  const source = String(safe.source || '')
  if (source === 'batch_result') return '分批结果校验'
  if (source === 'native_result') return '模型结果解析'
  if (source === 'model_upstream') return safe.phase === 'response_headers' ? '模型服务响应头' : '模型服务响应流'
  if (source === 'model_transport') return safe.phase === 'response_stream' || safe.phase === 'broker_stream' ? '模型响应传输' : '模型连接与传输'
  if (['native_protocol', 'cli_protocol', 'cli_result', 'cli_event'].includes(source)) return '原生响应协议'
  if (['native_process', 'native_runtime', 'native_persistence', 'executor_runtime'].includes(source)) return '原生执行环境'
  if (['native_gateway', 'executor_timeout', 'model_gateway', 'model_service', 'model_transport', 'model_protocol', 'model_http', 'local_limit', 'invalid_tool'].includes(source)) return '模型服务'
  if (source === 'publisher' || source === 'publish') return '发布'
  const code = String(safe.error_code || '')
  if (code.includes('INVALID_JSON')) return '模型结果解析'
  if (/HTTP|TLS|TIMEOUT|MODEL_|CONNECTION/.test(code)) return '模型服务'
  if (/EXECUTOR_(?:INVALID_JSON|NATIVE_PROTOCOL|CLI_PROTOCOL)/.test(code)) return '原生响应协议'
  if (code.includes('PUBLISH')) return '发布'
  return safe.cause_stage || safe.branch || safe.rejection_branch || ''
}
