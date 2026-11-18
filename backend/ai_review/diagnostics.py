"""只记录受控元数据；数据库不可写时仍通过容器日志保留最初失败。"""
import logging
import re
import sqlite3
import traceback
from datetime import datetime, timezone

from .common import encode

_IDENTIFIER = re.compile(r'^[A-Za-z0-9_.:/-]{1,180}$')
_TEXT_ENUMS = {
    'source', 'stage', 'phase', 'branch', 'cause_stage', 'call_id', 'request_id',
    'finish_reason', 'exception_type', 'category', 'rejection_branch', 'model_phase',
    'session_id', 'job', 'unit', 'sqlite_name', 'tls_phase', 'target_host', 'status',
    'error_code', 'executor_error_code', 'executor_phase', 'reason', 'extraction_path',
    'job_id', 'native_session_id', 'parser_reason', 'native_error_type', 'result_type',
    'event_type', 'error_type', 'transfer_phase', 'tls_exception_type',
    'upstream_error_type', 'upstream_error_code', 'upstream_request_id', 'upstream_trace_id',
    'error_sha256', 'captured_sha256', 'secondary_exception_type', 'thinking_protocol',
    'output_field', 'evidence_id', 'expected_output', 'received_output',
}
_INTEGERS = {
    'json_line', 'json_column', 'json_offset', 'response_bytes', 'exit_code',
    'http_status', 'upstream_http_status', 'sqlite_code', 'tls_errno', 'tls_verify_code',
    'diagnostic_version', 'received_bytes', 'received_chars', 'response_chars',
    'text_part_count', 'parts_count', 'limit_bytes',
    'response_limit_bytes', 'model_context_tokens', 'upstream_output_tokens', 'errno',
    'input_bytes', 'input_limit_bytes', 'body_limit_bytes', 'configured_output_tokens',
    'error_bytes', 'captured_bytes', 'declared_error_bytes', 'thinking_token_budget', 'reasoning_chars', 'content_chars',
    'finding_index', 'input_tokens', 'input_limit',
    'completed_calls', 'pending_bytes', 'stderr_bytes',
}
_BOOLEANS = {'completion_complete', 'done_seen', 'stream_closed', 'normalized_response',
             'error_truncated', 'thinking_enabled', 'turn_completed',
             'final_response_received', 'final_text_received'}
_FLOATS = {'task_remaining_seconds', 'elapsed_seconds', 'timeout_seconds', 'first_data_elapsed_seconds',
           'last_data_elapsed_seconds', 'headers_elapsed_seconds', 'request_started_at'}
_SENSITIVE = re.compile(r'(?i)(authorization|bearer|api[_ -]?key|token|secret|password|prompt|源码|正文)')
_EXTRACTION_PATHS = {
    'message.parts[type=text].text', 'session.create.response.id',
    'session.create.response', 'session.message.response.parts', 'session.message.response',
    'session.message.response.info.structured', 'session.message.response.info.error',
    'session.messages', 'event.data', 'global.health', 'cli.stdout.event',
    'cli.collected_event', 'cli.event.part.text', 'cli.events[type=text].part.text',
    'broker.response',
}
_PARSER_MESSAGES = {
    'expecting_value': '缺少 JSON 值', 'property_name_not_quoted': '对象字段名缺少双引号',
    'extra_data': 'JSON 后存在额外数据', 'unterminated_string': '字符串未结束',
    'invalid_escape': '字符串转义无效', 'invalid_control_character': '字符串含无效控制字符',
    'missing_colon': '对象字段缺少冒号', 'missing_comma': '数组或对象缺少逗号',
    'other_json_error': 'JSON 语法无效',
}
_CATEGORIES = {'磁盘空间不足', '内存不足', '权限拒绝', '数据库错误', '进程创建失败'}


def safe_remote(value):
    """只接受执行器约定的定位元数据，丢弃正文、任意消息和未知键。"""
    if not isinstance(value, dict):
        return {}
    result = {}
    if value.get('source') == 'git_source' and value.get('error_code') == 'GIT_SOURCE_FAILED':
        detail = value.get('git_error')
        if isinstance(detail, str) and len(detail) <= 1800:
            result['git_error'] = detail
    for key in _TEXT_ENUMS:
        item = value.get(key)
        if isinstance(item, str) and _IDENTIFIER.fullmatch(item):
            result[key] = item
    path = value.get('extraction_path')
    if isinstance(path, str) and path in _EXTRACTION_PATHS:
        result['extraction_path'] = path
    parser_reason = result.get('parser_reason')
    if parser_reason in _PARSER_MESSAGES:
        result['parser_message'] = _PARSER_MESSAGES[parser_reason]
    if isinstance(value.get('category'), str) and value['category'] in _CATEGORIES:
        result['category'] = value['category']
    for key in _INTEGERS:
        item = value.get(key)
        if type(item) is int and -(2**31) <= item <= 2**63-1:
            result[key] = item
    for key in _BOOLEANS:
        if type(value.get(key)) is bool:
            result[key] = value[key]
    for key in _FLOATS:
        item = value.get(key)
        limit = 10**11 if key == 'request_started_at' else 10**9
        if isinstance(item, (int, float)) and not isinstance(item, bool) and 0 <= item <= limit:
            result[key] = item
    reasons = value.get('finish_reasons')
    if isinstance(reasons, list):
        result['finish_reasons'] = [item for item in reasons[:20]
                                    if isinstance(item, str) and _IDENTIFIER.fullmatch(item)]
    timestamp = value.get('time_utc')
    if isinstance(timestamp, str) and len(timestamp) <= 40 and re.fullmatch(r'[0-9T:.+Z-]+', timestamp):
        result['time_utc'] = timestamp
    locations = value.get('frames') if isinstance(value.get('frames'), list) else [value.get('stack_location')]
    frames = []
    for location in locations[:6]:
        if not isinstance(location, dict):
            continue
        file_name, function, line = location.get('file'), location.get('function'), location.get('line')
        if (isinstance(file_name, str) and _IDENTIFIER.fullmatch(file_name)
                and isinstance(function, str) and _IDENTIFIER.fullmatch(function)
                and type(line) is int and 0 < line <= 10**8):
            frames.append({'file': file_name.rsplit('/', 1)[-1], 'function': function, 'line': line})
    if frames:
        result['frames'] = frames
    secondary = value.get('secondary')
    if isinstance(secondary, dict):
        safe_secondary = {}
        for key in ('error_code', 'source', 'phase', 'reason', 'exception_type',
                    'native_error_type', 'status', 'call_id', 'session_id'):
            item = secondary.get(key)
            if isinstance(item, str) and _IDENTIFIER.fullmatch(item):
                safe_secondary[key] = item
        if safe_secondary:
            result['secondary'] = safe_secondary
    return result


def safe_local_message(value):
    return value if isinstance(value, str) and len(value) <= 500 and not _SENSITIVE.search(value) else ''


def safe_frames(exc):
    return [{'file': frame.filename.rsplit('/', 1)[-1], 'line': frame.lineno, 'function': frame.name}
            for frame in traceback.extract_tb(exc.__traceback__)[-6:]]


def message_for(code, reason=None):
    if code == 'GIT_SOURCE_FAILED':
        details = {
            'credential_helper': 'Git 凭据助手无法运行，请核对 Worker 临时目录和运行环境',
            'authentication': 'Git 仓库认证失败，请核对审查机器人凭据及仓库读取权限',
            'tls_certificate': 'Git HTTPS 证书校验失败，请核对 Worker 的内网 CA 配置',
            'dns': 'Git 无法解析仓库主机名，请核对 Worker 的 DNS 和仓库地址',
            'connection': 'Git 无法连接仓库服务，请核对 Worker 到 Gitea 的网络和端口',
            'unsafe_directory': 'Git 拒绝读取所有权不匹配的仓库目录，请核对缓存目录属主',
            'permission_denied': 'Git 仓库目录不可写或执行权限不足，请核对 Worker 挂载权限',
            'missing_ref': 'Git 仓库缺少本次 PR 所需的引用或提交，请核对远端 PR 版本',
            'repository_not_found': 'Git 仓库地址不存在或不可读取，请核对仓库路径和读取权限',
            'repository_corrupt': 'Git 本地仓库或对象损坏，请在 Manager 清理该仓库缓存后重试',
            'disk_full': 'Git 源码存储空间不足，请清理磁盘或缓存',
            'redirect': 'Git 仓库地址发生重定向，请配置 Gitea 的实际访问地址',
        }
        return details.get(reason, 'Git 源码操作失败，原因未识别，请查看失败阶段、退出码和错误摘要')
    by_reason = {
        'responses_failed': '模型服务返回失败或未完成的 Responses 事件，请查看上游错误代码和结束原因',
        'missing_terminal': '模型响应流已结束，但缺少完整结束事件',
        'duplicate_terminal': '模型响应流包含重复的结束事件',
        'event_shape': '模型 Responses 事件结构无效',
        'unauthorized_tool': '模型返回了当前审查未启用的工具调用',
        'source_phase_mismatch': '模型返回了其他审查阶段的格式，审查未完成',
        'missing_structured_output': '模型未通过结构化结果工具提交审查结果，审查未完成',
        'structured_output_error': '模型提交的结构化审查结果不符合固定格式，审查未完成',
        'structured_result_not_object': '模型提交的结构化审查结果不是对象，审查未完成',
        'structured_result_too_large': '模型提交的结构化审查结果超过大小限制，审查未完成',
        'native_message_error': '原生模型消息执行失败，审查未完成',
        'upstream_http_error': '模型服务返回 HTTP 错误，审查未完成',
        'upstream_sse_error': '模型服务在响应流中返回错误，审查未完成',
        'upstream_json_error': '模型服务在响应中返回错误，审查未完成',
        'empty_result': '模型没有返回结果，审查未完成',
        'batch_result_not_text': '单元报告不是文本，审查未完成',
        'batch_thinking_unclosed': '单元输出的思考标签未闭合，审查未完成',
        'batch_fence_invalid': '单元报告的 JSON 代码围栏不完整或格式无效，审查未完成',
        'batch_evidence_shape': '单元证据引用必须为非空编号列表，审查未完成',
        'batch_evidence_unknown': '单元引用了当前批次不存在的证据编号，审查未完成',
        'batch_evidence_ambiguous': '单元证据编号对应多个位置，无法唯一核对，审查未完成',
        'json_syntax': '模型输出存在 JSON 语法错误，审查未完成',
        'result_not_object': '模型输出 JSON 顶层不是对象，审查未完成',
        'parts_not_list': '模型响应内容结构无效，审查未完成',
        'output_truncated': '模型输出被截断，审查未完成',
        'output_too_large': '模型输出超过大小限制，审查未完成',
        'unexpected_eof': '模型输出在 JSON 结束前中断，审查未完成',
    }
    if reason in by_reason:
        return by_reason[reason]
    known = {
        'TOKENIZER_RESOURCE': '计数资源缺失或校验失败，请核对离线资源包',
        'TOKENIZER_ENCODING': '消息编码失败，请核对计数型号与参数',
        'PR_TOTAL_CHANGE_LIMIT': 'PR 新增与删除合计超过该执行引擎的行数上限，未发送模型',
        'PR_CHANGE_LIMIT': 'PR 净增加超过 1000 行，当前单次审查不支持，未发送模型',
        'DIFF_UNSPLITTABLE': '输入包含无法安全拆分的超长行或元信息',
        'DIFF_COVERAGE': '分批文本覆盖校验失败，未调用模型',
        'PREPROCESS_TIMEOUT': '分批预处理超过时间预算，未调用模型',
        'PREPROCESS_RESOURCE': '分批预处理异常退出或超过内存预算',
        'BATCH_OUTPUT_INCOMPLETE': '单元输出被截断，审查未完成',
        'BATCH_OUTPUT_INVALID': '单元报告格式或证据引用无效，审查未完成',
        'BATCH_PHASE_INVALID': '执行单元与候选输入不一致，未发送模型',
        'BATCH_RECEIPT_UNKNOWN': '单元事件或结果回执尚未完整核对',
        'HISTORY_INCOMPLETE': '历史问题复核失败或回执未知，未正式发布',
        'MODEL_INPUT_BUDGET': '完整消息超过本单元输入额度，未发送模型',
        'invalid_json': '模型输出不是合法 JSON，无法完成审查',
        'MODEL_INVALID_JSON': '模型输出不是合法 JSON，无法完成审查',
        'MODEL_HTTP_ERROR': '模型服务返回错误，审查未完成',
        'MODEL_UPSTREAM_ERROR': '模型服务在响应中返回错误，审查未完成',
        'MODEL_TIMEOUT': '模型请求超时，审查未完成',
        'MODEL_TRANSPORT_ERROR': '模型响应传输失败，请查看传输阶段和已接收长度',
        'MODEL_GATEWAY_REJECTED': '模型网关拒绝本次请求，请查看任务授权及调用记录',
        'codex_failed': 'Codex 回合执行失败，具体事件已保存到原始诊断包',
        'codex_exit_failed': 'Codex 进程异常退出，请查看退出码及原始诊断包中的 stderr',
        'codex_response_incomplete': 'Codex 未返回完整审查结果，请查看模型完成次数与回合状态',
        'codex_review_json_invalid': 'Codex 审查结果无法解析为唯一完整 JSON，原文已保存在诊断包',
        'codex_event_json_invalid': 'Codex 执行事件格式无效，原文已保存在诊断包',
        'codex_review_shape_invalid': 'Codex 审查结果字段不符合约定，原文已保存在诊断包',
        'codex_output_limit': 'Codex 执行输出超过本任务上限，请查看已接收长度与上限',
        'codex_runtime_failed': 'Codex 运行时发生异常，请查看异常类型及执行器位置',
        'pr_agent_execution_failed': 'PR-Agent 执行失败，请查看异常类型和原始诊断包',
        'pr_agent_review_incomplete': 'PR-Agent 未覆盖全部变更或未返回完整结果',
        'pr_agent_context_incomplete': 'PR-Agent 上下文额度不足以容纳完整变更，请增大模型上下文窗口',
        'pr_agent_response_incomplete': 'PR-Agent 模型响应未完整结束，未发布审查结论',
        'pr_agent_review_shape_invalid': 'PR-Agent 审查结果缺少完整的问题列表',
        'pr_agent_priority_missing': 'PR-Agent 意见缺少明确的 P0/P1/P2/P3 级别',
        'pr_agent_priority_conflict': 'PR-Agent 意见的严重级别字段与标题不一致',
        'pr_agent_location_invalid': 'PR-Agent 意见位置无效，无法核对固定源码',
        'pr_agent_findings_limit': 'PR-Agent 输出达到问题数量上限，审查范围尚未确认完整',
        'pr_agent_version_mismatch': 'PR-Agent 版本与已验收版本不一致',
        'pr_agent_output_limit': 'PR-Agent 输出超过本任务正文大小上限',
        'deadline_exceeded': 'Codex 审查达到任务期限',
        'cli_exit': '原生审查进程异常退出',
        'aborted': '原生审查会话已中止',
        'unknown': '原生审查会话结果不确定',
        'native_message_error': '原生模型消息执行失败，审查未完成',
    }
    if code in known:
        return known[code]
    return '原生审查未完成（%s）' % code if isinstance(code, str) and _IDENTIFIER.fullmatch(code) else '原生审查未完成'


def describe(exc, job=None, phase='存储操作'):
    code = getattr(exc, 'sqlite_errorcode', None)
    primary = code & 255 if isinstance(code, int) else None
    category, message = {
        sqlite3.SQLITE_BUSY: ('REVIEW_DB_BUSY', '审查数据库繁忙，写入等待超时'),
        sqlite3.SQLITE_LOCKED: ('REVIEW_DB_LOCKED', '审查数据库连接发生锁冲突'),
        sqlite3.SQLITE_FULL: ('REVIEW_DB_FULL', '审查存储空间不足'),
        sqlite3.SQLITE_READONLY: ('REVIEW_DB_READONLY', '审查数据目录不可写'),
        sqlite3.SQLITE_IOERR: ('REVIEW_DB_IO', '审查存储读写失败'),
        sqlite3.SQLITE_CORRUPT: ('REVIEW_DB_CORRUPT', '审查数据库完整性异常'),
        sqlite3.SQLITE_NOTADB: ('REVIEW_DB_CORRUPT', '审查数据库格式异常'),
        sqlite3.SQLITE_SCHEMA: ('REVIEW_DB_SCHEMA', '审查数据库结构不一致'),
    }.get(primary, ('REVIEW_DB_ERROR', '审查数据库操作失败') if isinstance(exc, sqlite3.Error)
          else ('REVIEW_INTERNAL_ERROR', '审查内部操作失败'))
    # 不调用 format_exc，不保存异常参数、源码行或栈帧局部变量。
    frames = safe_frames(exc)
    value = {'error_code': category, 'message': message, 'phase': phase,
             'time_utc': datetime.now(timezone.utc).isoformat(), 'exception_type': type(exc).__name__,
             'sqlite_code': code, 'sqlite_name': getattr(exc, 'sqlite_errorname', None), 'frames': frames}
    if job:
        value.update(job=job.get('id'), unit=job.get('_unit_id'), model_phase=job.get('_phase'))
    logging.error('AI 审查诊断 %s', encode(value))
    return value
