"""运行时安全诊断：只保留定位元数据，不保留模型正文、源码或凭据。"""
import json
from pathlib import Path
import re
import traceback


_JSON_REASONS = (
    ('Expecting value', ('expecting_value', '缺少 JSON 值')),
    ('Expecting property name enclosed in double quotes',
     ('property_name_not_quoted', '对象字段名缺少双引号')),
    ('Extra data', ('extra_data', 'JSON 后存在额外数据')),
    ('Unterminated string starting at', ('unterminated_string', '字符串未结束')),
    ('Invalid \\escape', ('invalid_escape', '字符串转义无效')),
    ('Invalid control character at', ('invalid_control_character', '字符串含无效控制字符')),
    ("Expecting ':' delimiter", ('missing_colon', '对象字段缺少冒号')),
    ("Expecting ',' delimiter", ('missing_comma', '数组或对象缺少逗号')),
)
_TOKEN = re.compile(r'^[A-Za-z0-9_.:/-]{1,160}$')


def safe_token(value, fallback='unknown'):
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else fallback


def finish_reason(info):
    value = info.get('finish') if isinstance(info, dict) else None
    return safe_token(value, 'missing') if value else 'missing'


def native_error_type(info):
    value = info.get('error') if isinstance(info, dict) else None
    if isinstance(value, dict):
        value = value.get('name')
    return safe_token(value) if value else None


def json_failure(exc, *, source, phase, extraction_path, received_bytes,
                 response_chars=None, reason=None):
    parser_reason, parser_message = 'other_json_error', 'JSON 语法无效'
    message = getattr(exc, 'msg', '')
    for prefix, values in _JSON_REASONS:
        if isinstance(message, str) and message.startswith(prefix):
            parser_reason, parser_message = values
            break
    document = getattr(exc, 'doc', '')
    if not reason and received_bytes and isinstance(document, str):
        at_end = getattr(exc, 'pos', -1) >= len(document.rstrip())
        if at_end or parser_reason == 'unterminated_string':
            reason = 'unexpected_eof'
    return {
        'diagnostic_version': 1,
        'source': source,
        'phase': phase,
        'reason': reason or ('empty_result' if received_bytes == 0 else 'json_syntax'),
        'extraction_path': extraction_path,
        'received_bytes': received_bytes,
        'response_chars': response_chars,
        'json_offset': getattr(exc, 'pos', None),
        'json_line': getattr(exc, 'lineno', None),
        'json_column': getattr(exc, 'colno', None),
        'parser_reason': parser_reason,
        'parser_message': parser_message,
    }


def safe_exception(exc, *, source, phase, reason):
    frames = traceback.extract_tb(exc.__traceback__)
    location = None
    if frames:
        frame = frames[-1]
        location = {'file': Path(frame.filename).name, 'function': frame.name, 'line': frame.lineno}
    return {
        'diagnostic_version': 1,
        'source': source,
        'phase': phase,
        'reason': reason,
        'exception_type': safe_token(type(exc).__name__, 'Exception'),
        'stack_location': location,
    }


def safe_json_loads(raw, *, source, phase, extraction_path):
    received = len(raw) if isinstance(raw, (bytes, bytearray)) else len(str(raw).encode())
    try:
        return json.loads(raw), None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        if isinstance(exc, json.JSONDecodeError):
            diagnostic = json_failure(exc, source=source, phase=phase,
                                      extraction_path=extraction_path, received_bytes=received)
        else:
            diagnostic = safe_exception(exc, source=source, phase=phase, reason='invalid_encoding')
            diagnostic.update(extraction_path=extraction_path, received_bytes=received)
        return None, diagnostic
