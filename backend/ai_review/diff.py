"""严格解析累计补丁；只有旧文件可精确还原新文件时才产生锚点。"""
import re
from dataclasses import dataclass, field

from .common import ReviewError, digest, safe_path


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    rows: list = field(default_factory=list)


@dataclass
class Patch:
    old_path: str | None = None
    new_path: str | None = None
    hunks: list = field(default_factory=list)
    unsupported: str = ''


def git_path(raw):
    raw = raw.removesuffix(b'\t')
    if raw == b'/dev/null':
        return None
    if raw.startswith(b'"'):
        if not raw.endswith(b'"'):
            raise ReviewError('补丁路径引号不完整')
        data = bytearray()
        i = 1
        escapes = {ord('t'): 9, ord('n'): 10, ord('r'): 13, ord('b'): 8,
                   ord('f'): 12, ord('v'): 11, ord('a'): 7, 92: 92, 34: 34}
        while i < len(raw) - 1:
            if raw[i] != 92:
                data.append(raw[i]); i += 1
            elif i + 3 < len(raw) - 1 and re.fullmatch(rb'[0-7]{3}', raw[i+1:i+4]):
                data.append(int(raw[i+1:i+4], 8)); i += 4
            elif i + 1 < len(raw) - 1 and raw[i+1] in escapes:
                data.append(escapes[raw[i+1]]); i += 2
            else:
                raise ReviewError('补丁路径转义无效')
        raw = bytes(data)
    try:
        value = raw.decode('utf-8')
    except UnicodeError:
        raise ReviewError('文件路径不是 UTF-8') from None
    if not value.startswith(('a/', 'b/')):
        raise ReviewError('补丁缺少标准文件前缀')
    return safe_path(value[2:])


def parse_diff(data):
    if len(data) > 4 * 1024 * 1024:
        raise ReviewError('PR 补丁超过 4 MB，未执行完整审查')
    lines = file_lines(data)
    patches, patch, hunk = [], None, None
    for line in lines:
        if line.startswith(b'diff --git '):
            patch = Patch(); patches.append(patch); hunk = None
        elif patch is None:
            if line.strip():
                raise ReviewError('不是完整的 Git 统一补丁')
        elif line.startswith(b'@@ '):
            match = re.match(rb'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
            if not match:
                raise ReviewError('补丁修改区域格式错误')
            a, b, c, d = match.groups()
            hunk = Hunk(int(a), int(b or b'1'), int(c), int(d or b'1'))
            patch.hunks.append(hunk)
        elif hunk is not None and line[:1] in (b' ', b'+', b'-'):
            hunk.rows.append((line[:1].decode(), line[1:]))
        elif hunk is not None and line.startswith(b'\\ No newline at end of file'):
            if not hunk.rows or not hunk.rows[-1][1].endswith(b'\n'):
                raise ReviewError('补丁文件尾标记无效')
            kind, text = hunk.rows[-1]
            hunk.rows[-1] = (kind, text[:-1])
        elif hunk is not None:
            raise ReviewError('补丁包含未识别内容或被截断')
        elif line.startswith(b'--- '):
            patch.old_path = git_path(line[4:].removesuffix(b'\n'))
        elif line.startswith(b'+++ '):
            patch.new_path = git_path(line[4:].removesuffix(b'\n'))
        elif line.startswith((b'Binary files ', b'GIT binary patch', b'new file mode 120000',
                              b'new file mode 160000', b'old mode 120000', b'old mode 160000', b'index ')):
            if not line.startswith(b'index ') or line.rstrip().endswith((b'120000', b'160000')):
                patch.unsupported = '二进制文件、符号链接或子模块不产生行内意见'
        elif not line.startswith((b'index ', b'new file mode ', b'deleted file mode ', b'old mode ',
                                  b'new mode ', b'similarity index ', b'dissimilarity index ',
                                  b'rename from ', b'rename to ', b'copy from ', b'copy to ')):
            patch.unsupported = '该补丁类型不支持行内定位'
    for item in patches:
        if item.hunks and item.old_path is None and item.new_path is None:
            raise ReviewError('补丁缺少文件路径')
        for h in item.hunks:
            if sum(k != '+' for k, _ in h.rows) != h.old_count or sum(k != '-' for k, _ in h.rows) != h.new_count:
                raise ReviewError('补丁修改区域行数不完整')
    paths = [p.new_path or p.old_path for p in patches if p.hunks]
    if len(paths) != len(set(paths)):
        raise ReviewError('补丁包含重复文件，拒绝猜测归属')
    return patches


def file_lines(data):
    rows = data.split(b'\n')
    return [v + b'\n' for v in rows[:-1]] + ([rows[-1]] if rows[-1] else [])


def quote_text(raw):
    """模型引用不携带行终止符；代码缩进及其他空白保持原样。"""
    return raw.removesuffix(b'\n').removesuffix(b'\r').decode('utf-8')


def validate_patch(patch, old, new, snapshot_id):
    if patch.unsupported:
        return []
    if b'\0' in old or b'\0' in new:
        raise ReviewError('文件包含二进制内容')
    if (patch.old_path is None and old) or (patch.new_path is None and new):
        raise ReviewError('新增或删除文件的内容不匹配')
    source, target = file_lines(old), file_lines(new)
    output, anchors, cursor = [], [], 0
    for index, h in enumerate(patch.hunks):
        start = h.old_start - (1 if h.old_count else 0)
        new_start = h.new_start - (1 if h.new_count else 0)
        if start < cursor or start > len(source):
            raise ReviewError('补丁旧侧位置超界或重叠')
        output.extend(source[cursor:start])
        if len(output) != new_start:
            raise ReviewError('补丁新侧位置与累计变更不一致')
        cursor, right = start, new_start
        for kind, raw in h.rows:
            if kind != '+':
                if cursor >= len(source) or source[cursor] != raw:
                    raise ReviewError('补丁与固定版本旧文件不一致')
                cursor += 1
            if kind != '-':
                output.append(raw); right += 1
            if kind in ('+', '-'):
                side, number = ('new', right) if kind == '+' else ('old', cursor)
                try:
                    quote = quote_text(raw)
                except UnicodeError:
                    raise ReviewError('源码不是 UTF-8，不能精确引用') from None
                anchor = {'snapshot_id': snapshot_id, 'path': patch.new_path or patch.old_path,
                          'old_path': patch.old_path, 'new_path': patch.new_path, 'side': side,
                          'line': number, 'hunk': index, 'quote': quote}
                anchor['id'] = digest(anchor)[:32]
                anchors.append(anchor)
    output.extend(source[cursor:])
    if output != target:
        raise ReviewError('补丁无法完整还原固定版本新文件')
    return anchors


def locate(finding, anchors, snapshot_id):
    """只接受已发给该次模型任务的原始锚点；从不搜索近似文本补救。"""
    anchor = anchors.get(finding.get('anchor_id'))
    if not anchor:
        return None, '未返回本任务的有效锚点，已降级为普通意见'
    if anchor['snapshot_id'] != snapshot_id:
        return None, '锚点所属版本不一致'
    if finding.get('quote') != anchor['quote']:
        return None, '引用与该位置源码不完全一致，已降级为普通意见'
    if not anchor['quote'].strip():
        return None, '空白行不能承载缺陷证据'
    if anchor.get('ambiguous'):
        return None, '同一函数中存在重复的问题行文本，无法唯一确认代码块，已降级为普通意见'
    if not finding.get('location_verified'):
        return None, '反证检查未确认问题与该位置的对应关系'
    return anchor, ''
