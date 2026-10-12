"""固定提交的头文件缓存与大函数拆分；不执行仓库构建文件或代码。"""
import copy
import posixpath
import re

from .common import ReviewError, Stale, encode


class Headers:
    def __init__(self, client):
        self.client, self.cache, self.total = client, {}, 0

    def read(self, path, header, sha):
        key = (sha, path)
        if key in self.cache:
            code, error, actual = self.cache[key]
            if error: raise ReviewError(error)
            return actual, code
        if len(self.cache) >= 256 or self.total >= 8*1024*1024:
            self.cache.clear(); self.total = 0
            if hasattr(self,'views'):self.views.clear()
        actual = path
        try:
            try:
                raw = self.client.file(path, sha)
            except Stale:
                raise
            except ReviewError as exc:
                if str(exc) != '固定版本中缺少所需文件' or not hasattr(self.client, 'file_index'):
                    raise
                matches = [p for p in self.client.file_index(sha) if p == header or p.endswith('/'+header)]
                if len(matches) != 1:
                    raise ReviewError('头文件路径缺失或存在多个候选，无法确定包含路径')
                actual = matches[0]; raw = self.client.file(actual, sha)
            if self.total + len(raw) > 8*1024*1024:
                self.cache.clear(); self.total = 0
                if hasattr(self,'views'):self.views.clear()
            self.total += len(raw); code = raw.decode('utf-8')
            self.cache[key] = (code, '', actual)
            return actual, code
        except Stale:
            raise
        except (ReviewError, UnicodeError) as exc:
            if getattr(exc,'fatal_job',False):raise
            message = str(exc) if isinstance(exc, ReviewError) else '头文件不是 UTF-8'
            self.cache[key] = ('', message, actual)
            raise ReviewError(message) from None


def header_parts(header, used_code):
    from .snapshot import parse_source
    if len(header['code']) <= 12000:
        return [header], []
    raw, tree = parse_source(header['code'], header['path'])
    if tree.root_node.has_error:
        return [], [header['version']+' 大头文件无法可靠解析：'+header['path']]
    keywords = {
        'int','void','char','unsigned','signed','long','short','return','const','static','struct','enum',
        'volatile','sizeof','while','else','true','false','NULL','include','define','typedef','extern','if','for','switch','case','break'}
    names = set(re.findall(r'\b[A-Za-z_]\w*\b', used_code)) - keywords
    nodes = list(tree.root_node.named_children)
    selected = []
    # 有限固定点补齐声明引用的类型和宏；不按同名猜测多份条件实现。
    for _ in range(4):
        added = False
        for node in nodes:
            if node in selected: continue
            text = raw[node.start_byte:node.end_byte].decode()
            identifiers = set(re.findall(r'\b[A-Za-z_]\w*\b', text))
            if names & identifiers:
                selected.append(node); names.update(identifiers - keywords); added = True
        if not added: break
    result, size = [], 0
    for node in sorted(selected, key=lambda n: n.start_byte):
        code = raw[node.start_byte:node.end_byte].decode(); size += len(code)
        if size > 20000:
            return [], [header['version']+' 相关头文件声明仍超过单元预算：'+header['path']]
        from .common import digest
        item = dict(header, code=code, start=node.start_point.row+1, role='相关头文件声明')
        item['source_id'] = header['version']+':'+digest(item)[:20]
        result.append(item)
    return result, ([] if result else [header['version']+' 未找到可核对的相关声明：'+header['path']])


def split_large(item, file, snapshot, limit):
    """按真实语法语句分组；跨块上下文不足保留缺口，绝不声称整函数已被证明。"""
    from .snapshot import parse_source
    from .context import source, operations
    groups = {}
    for a in item['anchors']:
        groups.setdefault(a['hunk'], []).append(a)
    pending = [rows[i:i+24] for rows in groups.values() for i in range(0, len(rows), 24)]
    result = []
    for index, anchors in enumerate(pending):
        part = copy.deepcopy(item)
        part.update(anchors=anchors, operations=operations(snapshot, file, anchors), context=[])
        part['scope'] = dict(item['scope'], part=index+1, parts=len(pending))
        gaps = list(item['context_gaps'])
        for side, version in (('old','before'), ('new','after')):
            own = [a for a in anchors if a['side'] == side]
            if not own:
                # 只包含单侧的片段仍提供另一侧函数完整内容（能容纳时），否则明确缺口。
                original = item[version]
                if len(original['code']) > 14000:
                    part[version] = source(snapshot, original['path'], version, '未完整提供的另一侧函数', '')
                    gaps.append(version+' 大函数另一侧未完整提供，不能确认跨版本行为')
                continue
            raw, tree = parse_source(file[side], file['path'])
            lo, hi = min(a['line'] for a in own), max(a['line'] for a in own)
            selected = []
            def visit(node):
                if node.end_point.row+1 < lo or node.start_point.row+1 > hi: return
                if node.type.endswith(('_statement','_declaration')) and node.type != 'compound_statement':
                    if node.end_byte-node.start_byte <= 14000:
                        selected.append(node); return
                for child in node.named_children: visit(child)
            visit(tree.root_node)
            if not selected:
                gaps.append(version+' 大函数片段没有可可靠提取的完整语句')
                continue
            start = min(n.start_point.row+1 for n in selected); end = max(n.end_point.row+1 for n in selected)
            code = '\n'.join(file[side].split('\n')[start-1:end])
            part[version] = source(snapshot, item[version]['path'], version, '修改语法片段', code, start)
            gaps.append(version+' 超大函数已分段，其他语法块及跨块依赖未完整提供')
        part['context_gaps'] = list(dict.fromkeys(gaps))[:20]
        if len(encode(part)) <= limit:
            result.append(part)
    return result
