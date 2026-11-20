"""PR-Agent 的固定 Git 字节证据与 UTF-8 审查视图。"""
import base64
import hashlib
import re

from .common import ReviewError, safe_path
from .diff import file_lines

PROTOCOL = LOCAL_GIT_PROTOCOL = 2
CONFIG = 'source_encodings'


def validate_config(value):
    if not isinstance(value, dict) or len(value) > 100:
        raise ReviewError('源码备用编码须为目录与编码的映射，最多 100 项')
    for directory, codec in value.items():
        if directory != '.':
            safe_path(directory)
            if '\\' in directory:
                raise ReviewError('源码编码目录须使用仓库相对路径')
        if codec not in ('gbk', 'gb18030'):
            raise ReviewError('源码备用编码仅支持 GBK 和 GB18030')
    return value


def pack(raw):
    return base64.b64encode(raw).decode('ascii')


def unpack(snapshot):
    raw = base64.b64decode(snapshot['raw_diff'], validate=True)
    if hashlib.sha256(raw).hexdigest() != snapshot['diff_sha256']:
        raise ReviewError('固定 PR 补丁原始摘要不一致')
    return raw


def blocks(raw):
    return [part for part in re.split(rb'(?m)(?=^diff --git )', raw) if part.strip()]


def decode_blob(raw, path, side, rules):
    if b'\0' in raw:
        raise ReviewError(f'{path}（{side}）含二进制内容')
    try:
        return raw.decode('utf-8'), 'utf-8'
    except UnicodeDecodeError as exc:
        offset = exc.start
    matches = [key for key in rules if key == '.' or path.startswith(key + '/')]
    preferred = rules[max(matches, key=lambda key: 0 if key == '.' else len(key))] if matches else None
    if preferred:
        try:
            decoded = raw.decode(preferred)
            if decoded.encode(preferred) == raw:
                return decoded, preferred
        except UnicodeError as exc:
            if isinstance(exc, UnicodeDecodeError):
                offset = exc.start
    line = raw.count(b'\n', 0, offset) + 1
    column = offset - raw.rfind(b'\n', 0, offset)
    raise ReviewError(f'{path}（{side}）第 {line} 行、第 {column} 字节无法无损解码（{preferred or "未配置备用编码"}）')


def render_block(raw, patch, encodings):
    """沿原有 hunk 结构转换正文。原始字节校验在调用此函数前完成。"""
    output = []
    hunks = iter(patch.hunks)
    rows = iter(())
    in_hunk = False
    for line in file_lines(raw):
        if line.startswith(b'@@ '):
            rows = iter(next(hunks).rows)
            in_hunk = True
            # 函数名只是 Git 提示，旧侧及新侧的编码可以不同。
            try:
                text = line.decode('utf-8')
            except UnicodeDecodeError:
                try:
                    text = line.decode(encodings['old'])
                except UnicodeDecodeError:
                    text = line.decode(encodings['new'])
        elif in_hunk and line[:1] in (b' ', b'+', b'-'):
            kind, source = next(rows)
            side = 'new' if kind == '+' else 'old'
            decoded = source.decode(encodings[side])
            if kind == ' ' and decoded != source.decode(encodings['new']):
                raise ReviewError('补丁上下文在新旧侧的解码结果不同')
            text = kind + decoded + ('\n' if line.endswith(b'\n') and not source.endswith(b'\n') else '')
        else:
            text = line.decode('utf-8')
        output.append(text)
    return ''.join(output)
