"""统一模型结束语义；仅恢复具有完整 JSON 边界的对象，不猜补内容。"""
import json
import io

DEFAULT_BODY_MIB = 8
MAX_BODY_MIB = 64


class CompletionError(ValueError):
    def __init__(self, code, metadata=None):
        self.code = code
        self.metadata = dict(metadata or {}, rejection_branch=code)
        super().__init__(code)


def body_limit(config):
    return config.get('model_response_max_mib', DEFAULT_BODY_MIB) * 1024 * 1024


def safe_usage(value):
    return {k: v for k, v in (value.items() if isinstance(value, dict) else [])
            if k in ('prompt_tokens', 'completion_tokens', 'total_tokens') and type(v) is int and v >= 0}


class ResponseCollector:
    """SSE 按事件消耗，心跳和 usage 不累计占用正文内存。"""
    def __init__(self, streaming, limit):
        self.streaming, self.limit = streaming, limit
        self.pending = bytearray(); self.event = []
        self.parts = io.StringIO(); self.size = 0; self.tools = {}; self.reasons = []
        self.metadata = {'done_frames': 0, 'usage': {}, 'tool_types': [], 'streaming': streaming}

    def feed(self, data):
        self.pending.extend(data)
        if not self.streaming:
            if len(self.pending) > self.limit * 8:
                raise CompletionError('response_envelope_limit', {'limit_bytes': self.limit * 8})
            return
        while True:
            end = self.pending.find(b'\n')
            if end < 0: break
            line = bytes(self.pending[:end]).rstrip(b'\r'); del self.pending[:end+1]
            if not line:
                self.dispatch()
            elif line.startswith(b'data:'):
                self.event.append(line[5:].lstrip(b' '))
            if sum(map(len, self.event)) > self.limit * 8:
                raise CompletionError('response_event_limit', {'limit_bytes': self.limit * 8})
        if len(self.pending) > self.limit * 8:
            raise CompletionError('response_event_limit', {'limit_bytes': self.limit * 8})

    def dispatch(self):
        if not self.event: return
        data = b'\n'.join(self.event); self.event.clear()
        if data == b'[DONE]':
            self.metadata['done_frames'] += 1
            return
        try: self.consume(json.loads(data), True)
        except CompletionError: raise
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
            self.metadata['malformed_event'] = True

    def consume(self, value, streaming):
        if not isinstance(value, dict): raise ValueError()
        self.metadata['usage'].update(safe_usage(value.get('usage')))
        if value.get('error'):
            self.metadata['upstream_error_event'] = True
        choices = value.get('choices', [])
        if not isinstance(choices, list): raise ValueError()
        if len(choices) > 1: self.metadata['multiple_choices'] = True
        for choice in choices[:1]:
            if streaming and choice.get('index', 0) != 0:
                self.metadata['multiple_choices'] = True
                continue
            reason = choice.get('finish_reason')
            if reason is not None:
                if reason not in self.reasons and len(self.reasons) < 8:
                    self.reasons.append(reason if reason in ('stop','length','tool_calls','function_call','content_filter','error') else 'unknown')
            message = choice.get('delta' if streaming else 'message') or {}
            content = message.get('content')
            if isinstance(content, str):
                self.size += len(content.encode())
                if self.size > self.limit:
                    raise CompletionError('response_body_limit', {'body_bytes': self.size, 'limit_bytes': self.limit})
                if content: self.parts.write(content)
            elif content is not None:
                self.metadata['unsupported_content'] = True
            tools = message.get('tool_calls') or []
            if message.get('function_call'):
                tools = [{'index': 0, 'function': message['function_call']}]
            if not isinstance(tools, list): raise ValueError()
            for position, tool in enumerate(tools):
                index = tool.get('index', position)
                if type(index) is not int or index < 0: raise ValueError()
                if index not in self.tools:
                    self.size += 64  # 空工具对象也占用结构化正文资源。
                    if self.size > self.limit:
                        raise CompletionError('response_body_limit', {'body_bytes': self.size, 'limit_bytes': self.limit})
                target = self.tools.setdefault(index, {'name': '', 'arguments': ''})
                function = tool.get('function', {})
                for field in ('name', 'arguments'):
                    text = function.get(field, '')
                    if not isinstance(text, str): raise ValueError()
                    self.size += len(text.encode())
                    if self.size > self.limit:
                        raise CompletionError('response_body_limit', {'body_bytes': self.size, 'limit_bytes': self.limit})
                    target[field] += text

    def finish(self):
        if self.streaming:
            if self.pending:
                line = bytes(self.pending).rstrip(b'\r')
                if line.startswith(b'data:'): self.event.append(line[5:].lstrip(b' '))
                self.pending.clear()
            self.dispatch()
        else:
            try: self.consume(json.loads(self.pending), False)
            except CompletionError: raise
            except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
                raise CompletionError('invalid_envelope') from None
        self.metadata.update(finish_reasons=self.reasons, body_bytes=self.size,
                             done_seen=bool(self.metadata['done_frames']),
                             tool_types=list(dict.fromkeys(t['name'] if t['name'] in ('find_files','search_code','read_file') else 'unsupported' for t in self.tools.values())))
        message = {'content': self.parts.getvalue()}
        if self.tools:
            message['tool_calls'] = [{'type':'function','function':v} for v in self.tools.values()]
        return {'choices':[{'index':0,'finish_reason':self.reasons[-1] if self.reasons else None,'message':message}],
                'usage':self.metadata['usage']}, self.metadata


def recover_objects(text):
    """只遍历顶层成员和完整数组元素，避免从说明文字中误取 JSON。"""
    decoder = json.JSONDecoder(); pos = 0; recovered = {}
    def skip(index):
        while index < len(text) and text[index].isspace(): index += 1
        return index
    pos = skip(pos)
    if text[pos:pos+1] != '{': return recovered
    pos += 1
    while True:
        try:
            pos = skip(pos); key, pos = decoder.raw_decode(text, pos)
            pos = skip(pos)
            if not isinstance(key, str) or text[pos:pos+1] != ':': break
            pos = skip(pos+1)
            if key in ('findings','checks','context_requests') and text[pos:pos+1] == '[':
                rows = []; pos = skip(pos+1)
                while text[pos:pos+1] != ']':
                    item, end = decoder.raw_decode(text, pos)
                    if isinstance(item, dict): rows.append(item)
                    recovered[key] = rows
                    pos = skip(end)
                    if text[pos:pos+1] != ',': break
                    pos = skip(pos+1)
                recovered[key] = rows
                if text[pos:pos+1] != ']': break
                pos += 1
            else:
                _, pos = decoder.raw_decode(text, pos)
            pos = skip(pos)
            if text[pos:pos+1] != ',': break
            pos += 1
        except (ValueError, TypeError): break
    return {k:v for k,v in recovered.items() if v}


def interpret(envelope, metadata=None):
    metadata = dict(metadata or {})
    try:
        choice = envelope['choices'][0]; message = choice['message']
        text = message.get('content') or ''
        reason = choice.get('finish_reason')
    except (TypeError, KeyError, IndexError): raise CompletionError('invalid_envelope', metadata) from None
    metadata.setdefault('finish_reasons', [reason] if reason is not None else [])
    warnings = []
    complete = reason == 'stop' and all(r == 'stop' for r in metadata.get('finish_reasons', [])) and not any(metadata.get(k) for k in
        ('malformed_event','upstream_error_event','multiple_choices','unsupported_content'))
    if not complete: warnings.append('模型未确认完整结束，已保留可解析结果')
    if metadata.get('streaming') and not metadata.get('done_seen'):
        warnings.append('模型流缺少结束帧，已按实际正文解析')
    try:
        value = json.loads(text) if text.strip() else {}
        if not isinstance(value, dict): raise ValueError()
    except ValueError as exc:
        metadata['json_offset'] = getattr(exc, 'pos', None)
        value = recover_objects(text)
        complete = False; warnings.append('JSON 正文不完整，仅保留边界明确的完整对象')
    requests = list(value.get('context_requests', [])) if isinstance(value.get('context_requests'), list) else []
    for tool in message.get('tool_calls') or []:
        function = tool.get('function', {})
        name = function.get('name')
        try:
            args = json.loads(function.get('arguments', ''))
            if name not in ('find_files','search_code','read_file') or not isinstance(args, dict) or args.get('version') not in ('before','after') or 'op' in args:
                raise ValueError()
            requests.append(dict(args, op=name))
        except (ValueError, TypeError):
            complete = False; warnings.append('模型请求了无法转换的工具，未执行该工具')
    if requests: value['context_requests'] = requests
    if not value or not any(isinstance(value.get(k), list) for k in ('findings','checks','context_requests')):
        raise CompletionError('no_usable_result', dict(metadata, completion_warnings=warnings, json_offset=metadata.get('json_offset')))
    # 只读工具请求是中间步骤，不表示本单元的最终审查已经完整。
    if requests and not any(metadata.get(k) for k in ('malformed_event','upstream_error_event','multiple_choices','unsupported_content')) and all(t == 'read_file' or t == 'search_code' or t == 'find_files' for t in metadata.get('tool_types', [])) and reason == 'tool_calls':
        complete = True; warnings = [w for w in warnings if w != '模型未确认完整结束，已保留可解析结果']
    metadata.update(completion_complete=complete, completion_warnings=list(dict.fromkeys(warnings)), rejection_branch=None)
    return value, metadata
