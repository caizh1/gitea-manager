"""有界源码供给：版本方向固定，函数配对唯一，保留缺失证据而不推测。"""
import posixpath
import re
from bisect import bisect_left, bisect_right
from .common import ReviewError, Stale, digest, encode
from .snapshot import functions, comment_only_lines, parse_source

MAX_INPUT = 48000
OPERATIONS = {'call_expression', 'assignment_expression', 'update_expression', 'binary_expression',
              'subscript_expression', 'pointer_expression', 'return_statement'}


def source(snapshot, path, version, role, code, start=1):
    value = {'path': path, 'version': version, 'role': role, 'start': start, 'code': code}
    value['source_id'] = version + ':' + digest([snapshot['id'], value])[:20]
    return value


def operations(snapshot, file, anchors, parsed=None):
    """提供真实语法操作及其变更成员；模型选择邻行或声明别名时不自动补救。"""
    result = []
    kinds = OPERATIONS | ({'init_declarator','preproc_def','preproc_function_def','type_definition'}
                          if snapshot.get('analysis_version',0)>=4 else set())
    for side in ('old', 'new'):
        raw, tree = parsed[side] if parsed else parse_source(file[side], file['path'], file.get('parser_language'))
        own = sorted((a for a in anchors if a['side'] == side),key=lambda a:a['line'])
        lines = [a['line'] for a in own]
        pending = [tree.root_node]
        while pending:
            node = pending.pop()
            end_line=node.end_point.row+1
            if snapshot.get('analysis_version',0)>=4 and node.end_point.column==0 and node.end_point.row>node.start_point.row:
                end_line-=1
            lo=bisect_left(lines,node.start_point.row+1);hi=bisect_right(lines,end_line)
            matched = [a['id'] for a in own[lo:hi]]
            if not matched:
                continue
            if node.type in kinds and not node.has_error:
                quote = raw[node.start_byte:node.end_byte].decode()
                if len(quote) <= 2000:
                    value = {'side': side, 'kind': node.type, 'start': node.start_point.row+1,
                             'end': end_line, 'quote': quote, 'anchor_ids': matched,
                             'start_byte': node.start_byte, 'end_byte': node.end_byte}
                    value['id'] = digest([snapshot['id'], file['path'], value])[:24]
                    result.append(value)
            pending.extend(reversed(node.named_children))
    return result


def _symbols(code):
    # 按实际成员关系补齐跨入口的状态读写者，而非只收集直接调用函数。
    return set(re.findall(r'(?:->|\.)\s*([A-Za-z_]\w*)', code))


def _name(f):
    return next(iter(re.findall(r'\b([A-Za-z_]\w*)\s*\(', f['name'])), '')


def build_items(snapshot, client=None):
    from .context_parts import Headers, header_parts, split_large
    header_cache = Headers(client) if client else None
    items, skipped = [], []
    for file in snapshot['files']:
        scopes, errors = {}, {}
        for side in ('old', 'new'):
            scopes[side], errors[side] = functions(file[side], file['path'])
        non_code = {s: comment_only_lines(file[s], file['path']) for s in scopes}
        by_name = {s: {} for s in scopes}
        for side in scopes:
            for f in scopes[side]:
                by_name[side].setdefault(f['name'], []).append(f)
        groups = {}
        for aid in file['anchors']:
            a = snapshot['anchors'][aid]
            if a['line'] in non_code[a['side']]:
                continue
            matches = [f for f in scopes[a['side']] if f['start'] <= a['line'] <= f['end']]
            name = matches[0]['name'] if len(matches) == 1 else '文件级修改'
            if any(len(by_name[s].get(name, [])) > 1 for s in scopes):
                skipped.append({'path': file['path'], 'reason': '同名函数归属存在歧义，需人工审查'})
                continue
            groups.setdefault(name, []).append(a)
        declarations = {}
        for side in scopes:
            declarations[side] = '\n'.join(line if not any(f['start'] <= i <= f['end'] for f in scopes[side]) else ''
                                             for i, line in enumerate(file[side].split('\n'), 1))
        headers, header_gaps = [], []
        include_keys = []
        for side, version, sha in (('old', 'before', snapshot.get('merge_base')), ('new', 'after', snapshot.get('head'))):
            source_path = file.get('old_path' if side == 'old' else 'new_path') or file['path']
            for header in dict.fromkeys(re.findall(r'^\s*#\s*include\s*"([^"\n]+)"', file[side], re.M)):
                path = posixpath.normpath(posixpath.join(posixpath.dirname(source_path), header))
                if header not in include_keys:
                    include_keys.append(header)
                if not client or path.startswith(('../', '/')):
                    header_gaps.append(version+' 未读取本地头文件：'+path)
                    continue
                try:
                    actual, code = header_cache.read(path, header, sha)
                    headers.append(source(snapshot, actual, version, '头文件', code))
                except Stale:
                    raise
                except (ReviewError, UnicodeError) as exc:
                    header_gaps.append(version+' '+str(exc)+'：'+path)
        for name, anchors in groups.items():
            own = {s: next(iter(by_name[s].get(name, [])), None) for s in scopes}
            if any(f and f['parse_error'] for f in own.values()) or name == '文件级修改' and any(errors.values()):
                skipped.append({'path': file['path'], 'reason': '修改范围语法解析不完整，需人工审查'})
                continue
            primary = {s: own[s]['code'] if own[s] else declarations[s] if name == '文件级修改' else '' for s in scopes}
            item = {'analysis_version': 2, 'snapshot_id': snapshot['id'], 'path': file['path'],
                    'scope': {'name': name, 'start': (own['new'] or own['old'] or {}).get('start', 1),
                              'end': (own['new'] or own['old'] or {}).get('end', 1)},
                    'before': source(snapshot, file.get('old_path') or file['path'], 'before', '修改函数', primary['old'], (own['old'] or {}).get('start', 1)),
                    'after': source(snapshot, file.get('new_path') or file['path'], 'after', '修改函数', primary['new'], (own['new'] or {}).get('start', 1)),
                    'context': [], 'context_gaps': list(header_gaps), 'parse_warning': any(errors.values()),
                    'anchors': anchors, 'operations': operations(snapshot, file, anchors)}
            for a in anchors:
                a['ambiguous'] = sum(line.removesuffix('\r') == a['quote'] for line in primary[a['side']].split('\n')) > 1
            if len(encode(item)) > MAX_INPUT:
                parts = split_large(item, file, snapshot, MAX_INPUT)
                covered = {a['id'] for part in parts for a in part['anchors']}
                for part in parts:
                    items.append(part)
                    skipped.append({'path': file['path'], 'scope': name, 'reason': '大函数分段上下文不足：'+'；'.join(part['context_gaps'])})
                if covered != {a['id'] for a in anchors}:
                    skipped.append({'path': file['path'], 'scope': name, 'reason': '部分超大语法块无法安全拆分，需人工核对'})
                continue
            choices = []
            for side, version in (('old', 'before'), ('new', 'after')):
                if name != '文件级修改':
                    choices.append((100, source(snapshot, file['path'], version, '声明和接口契约', declarations[side])))
                symbols = _symbols(primary[side])
                primary_name = _name(own[side]) if own[side] else ''
                direct = [f for f in scopes[side] if f != own[side] and
                          (re.search(r'\b'+re.escape(_name(f))+r'\s*\(', primary[side]) or
                           primary_name and re.search(r'\b'+re.escape(primary_name)+r'\s*\(', f['code']))]
                linked = symbols | set().union(*(_symbols(f['code']) for f in direct)) if direct else symbols
                for f in scopes[side]:
                    if f == own[side]:
                        continue
                    rank = 80 if f in direct else 60 if linked & _symbols(f['code']) else 10
                    if rank > 10:
                        choices.append((rank, source(snapshot, file['path'], version, f['name'], f['code'], f['start'])))
            for header in headers:
                pieces, gaps = header_parts(header, primary['old']+'\n'+primary['new'])
                choices.extend((90, piece) for piece in pieces)
                item['context_gaps'].extend(gaps)
            for _, candidate in sorted(choices, key=lambda p: -p[0]):
                item['context'].append(candidate)
                if len(encode(item)) > MAX_INPUT - 3000:
                    item['context'].pop()
                    item['context_gaps'].append(candidate['version']+' 未提供：'+candidate['path']+' '+candidate['role'])
            # 缺口也有界：不能让异常大文件通过缺口列表绕过输入预算。
            if len(item['context_gaps']) > 20:
                item['context_gaps'] = item['context_gaps'][:20]+['其他上下文超过预算，未逐项列出']
            if len(encode(item)) > MAX_INPUT:
                skipped.append({'path': file['path'], 'reason': '上下文及缺口记录超过单元预算'})
                continue
            if item['context_gaps']:
                skipped.append({'path': file['path'], 'scope': name, 'reason': '函数上下文覆盖不足：'+'；'.join(item['context_gaps'])})
            items.append(item)
    snapshot['coverage']['skipped'].extend(skipped)
    snapshot['coverage']['complete'] = not snapshot['coverage']['skipped']
    return items
