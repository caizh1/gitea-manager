"""固定版本的语法关系；不以共享单词推导调用或别名。"""
from collections import defaultdict

from .common import ReviewError, Stale, digest
from .diff import parse_diff
from .syntax_context import View


def capture_sources(client, snapshot, active=lambda: None):
    """只读取改动文件的两版 Git 对象；不修改工作仓库或追踪移动分支。"""
    sources, limitations, total = [], [], 0
    previous = getattr(client, 'max_file_bytes', 128 * 1024)
    client.max_file_bytes = 2 * 1024 * 1024
    try:
        for patch in parse_diff(snapshot['diff'].encode()):
            path = patch.new_path or patch.old_path
            if not path or not path.lower().endswith(('.c', '.h', '.cc', '.cpp', '.hpp')):
                limitations.append({'path': path, 'reason': '未提供该语言的跨批语法分析；主审仍覆盖完整文本变更'})
                continue
            for side, name, version in (('old', patch.old_path, snapshot['merge_base']),
                                        ('new', patch.new_path, snapshot['head'])):
                if not name:
                    continue
                if total >= 16 * 1024 * 1024:
                    limitations.append({'path': path, 'side': side, 'reason': '固定源码上下文总量已达到 16 MiB，未继续读取'})
                    continue
                active()
                try:
                    raw = client.file(name, version)
                    total += len(raw)
                    if total > 16 * 1024 * 1024:
                        raise ReviewError('固定源码上下文总量超过 16 MiB')
                    text = raw.decode('utf-8')
                    sources.append({'path': path, 'source_path': name, 'side': side,
                                    'version': version, 'sha256': __import__('hashlib').sha256(raw).hexdigest(),
                                    'text': text})
                except Stale:
                    raise
                except (ReviewError, UnicodeError) as exc:
                    limitations.append({'path': path, 'side': side, 'reason': str(exc)})
    finally:
        client.max_file_bytes = previous
    return sources, limitations


def _walk(node):
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _name(view, node):
    while node is not None:
        if node.type in ('identifier', 'field_identifier'):
            return view.code(node)
        node = node.child_by_field_name('declarator')
    return None


def analyze(snapshot, rows):
    """函数、直接调用及可解析状态访问；每条关系都有确定源码范围。"""
    scopes, definitions, limitations = {}, defaultdict(list), list(snapshot.get('analysis_limitations', []))
    # 源码行优先复用原 diff 证据；其余只读上下文拥有独立编号，不能作为行内定位。
    by_line = {(r['path'], side, r[side]): r for r in rows for side in ('old', 'new') if r[side] is not None}
    views = [(source, View(source['text'], source['path'])) for source in snapshot.get('analysis_sources', [])]

    def source_rows(source, view, start, end):
        result = []
        for n in range(start + 1, end + 1):
            row = by_line.get((source['path'], source['side'], n))
            text = view.lines[n - 1]
            if row is not None and row['text'].rstrip('\r\n') != text.rstrip('\r'):
                raise ReviewError('固定源码与 diff 行内容不一致，不能建立证据映射')
            result.append(row or {'id': 'S' + digest([source['path'], source['version']])[:12] + 'L' + str(n),
                'path': source['path'], 'old_path': source['source_path'], 'new_path': source['source_path'],
                'hunk': None, 'kind': 'context', 'old': n if source['side'] == 'old' else None,
                'new': n if source['side'] == 'new' else None, 'text': text + '\n', 'anchor_id': None,
                'version': source['version'], 'source_sha256': source['sha256']})
        return result

    declarations, changed_declarations = defaultdict(list), {}
    for source, view in views:
        for node in view.nodes:
            if node.type not in ('declaration', 'type_definition', 'preproc_def', 'enum_specifier',
                                 'struct_specifier', 'union_specifier', 'class_specifier'):
                continue
            parent = node.parent
            if parent is not None and parent.type not in ('translation_unit', 'preproc_ifdef', 'preproc_if', 'preproc_else'):
                continue
            names = view.declared(node)
            names.update(view.code(n.child_by_field_name('name')) for n in _walk(node)
                         if n.type in ('struct_specifier', 'union_specifier', 'class_specifier')
                         and n.child_by_field_name('name') is not None and n.child_by_field_name('body') is not None)
            names.update(view.code(n.child_by_field_name('name')) for n in _walk(node)
                         if n.type == 'enumerator' and n.child_by_field_name('name') is not None)
            refs = {view.code(n) for n in _walk(node) if n.type in ('identifier', 'type_identifier')}
            for name in names:
                declarations[(source['side'], name)].append((source, view, node, refs))
            evidence = source_rows(source, view, *view.span(node))
            if names and any(r['kind'] in ('+', '-') for r in evidence):
                changed_lines = {r[source['side']] for r in evidence if r['kind'] in ('+', '-')}
                members = set()
                for field in _walk(node):
                    if field.type != 'field_declaration':
                        continue
                    lo, hi = view.span(field)
                    if changed_lines & set(range(lo + 1, hi + 1)):
                        members.update(view.code(n) for n in _walk(field) if n.type == 'field_identifier')
                ident = digest(['declaration', source['path'], source['side'], node.start_byte, node.end_byte])[:24]
                changed_declarations[ident] = {'id': ident, 'name': ' / '.join(sorted(names)),
                    'symbols': sorted(names), 'path': source['path'], 'side': source['side'],
                    'members': sorted(members),
                    'scope_type': 'declaration', 'body_ids': [r['id'] for r in evidence],
                    'start': view.span(node)[0] + 1, 'end': view.span(node)[1], 'evidence': evidence,
                    'calls': [], 'reads': [], 'writes': [], 'allocates': [], 'frees': [], 'static': True}
    for source, view in views:
        side, path = source['side'], source['path']
        first_function = min((node.start_byte for _, node in view.functions), default=0)
        file_comments = [node for node in view.nodes if node.type == 'comment' and node.end_byte <= first_function]
        if view.errors:
            limitations.append({'path': path, 'side': side, 'reason': '存在宏或无法解析的语法；关系分析不证明不存在其他关联'})
        globals_ = {}
        for child in view.tree.root_node.named_children:
            if child.type == 'declaration':
                for name in view.declared(child):
                    globals_[name] = f"全局:{path if 'static' in view.code(child).split() else ''}:{name}"
        for _, node in view.functions:
            declarator = node.child_by_field_name('declarator')
            name = _name(view, declarator)
            if not name:
                limitations.append({'path': path, 'side': side, 'reason': '函数声明无法唯一提取名称，未推断其跨函数关系'})
                continue
            start, end = view.span(node)
            ident = digest([path, side, name, start, end])[:24]
            evidence = source_rows(source, view, start, end)
            body_ids = [r['id'] for r in evidence]
            # 文件级契约和函数紧邻文档属于判断前提，不能只留给第一批。
            for comment in file_comments:
                evidence.extend(source_rows(source, view, *view.span(comment)))
            before = node.start_byte
            for comment in sorted((n for n in view.nodes if n.type == 'comment' and n.end_byte <= before),
                                  key=lambda n: n.end_byte, reverse=True):
                if view.raw[comment.end_byte:before].strip():
                    break
                evidence.extend(source_rows(source, view, *view.span(comment)))
                before = comment.start_byte
            refs = {view.code(n) for n in _walk(node) if n.type in ('identifier', 'type_identifier')}
            direct_refs = set(refs)
            visited = set()
            while refs - visited:
                key = min(refs - visited)
                visited.add(key)
                for src, other, declaration, dependencies in declarations.get((side, key), []):
                    evidence.extend(source_rows(src, other, *other.span(declaration)))
                    refs.update(dependencies)
            evidence = list({r['id']: r for r in evidence}.values())
            variables, local_names = {}, set()
            for part in _walk(node):
                if part.type in ('parameter_declaration', 'declaration'):
                    names = view.declared(part)
                    local_names.update(names)
                    type_node = part.child_by_field_name('type')
                    if type_node is not None:
                        type_name = view.code(type_node.child_by_field_name('name') or type_node)
                        matches = {(src['path'], decl.start_byte) for src, _, decl, _ in declarations.get((side, type_name), [])}
                        if len(matches) == 1:
                            # 同名类型必须绑定到同一可核对声明，不能仅因拼写一样跨文件关联。
                            binding = type_name + '@' + digest(sorted(matches))[:12]
                            variables.update({n: binding for n in names})
            reads, writes, allocates, frees, calls, indirect = set(), set(), set(), set(), [], False

            def object_key(part):
                if part is None:
                    return None
                if part.type == 'pointer_expression' and view.code(part).lstrip().startswith('&'):
                    return object_key(part.child_by_field_name('argument'))
                if part.type == 'identifier':
                    value = view.code(part)
                    return globals_.get(value) if value not in local_names else None
                if part.type == 'field_expression':
                    argument = part.child_by_field_name('argument')
                    field = part.child_by_field_name('field')
                    if argument is not None and argument.type == 'identifier' and field is not None:
                        typ = variables.get(view.code(argument))
                        if typ:
                            return '字段:' + typ + '.' + view.code(field)
                    prefix = object_key(argument)
                    if prefix and field is not None:
                        return prefix + '.' + view.code(field)
                return None

            for part in _walk(node):
                if part.type == 'call_expression':
                    callee = part.child_by_field_name('function')
                    if callee is not None and callee.type == 'identifier':
                        called = view.code(callee)
                        calls.append({'name': called, 'line': part.start_point.row + 1})
                        args = part.child_by_field_name('arguments')
                        if called in ('free', 'g_free', 'kfree') and args is not None:
                            frees.update(key for arg in args.named_children if (key := object_key(arg)))
                        if called in ('memset', 'memcpy', 'memmove') and args is not None and args.named_children:
                            destination = object_key(args.named_children[0])
                            if destination:
                                writes.add(destination)
                    else:
                        indirect = True
                key = object_key(part)
                if not key:
                    continue
                parent = part.parent
                if parent is not None and parent.type == 'field_expression' and parent.child_by_field_name('argument') == part:
                    continue  # 只记录完整成员路径，不把 u->ext.* 合并成共享 u->ext。
                target = part
                while parent is not None and (parent.type == 'parenthesized_expression' or
                        parent.type == 'subscript_expression' and parent.child_by_field_name('argument') == target):
                    target, parent = parent, parent.parent
                write = parent is not None and (parent.type == 'update_expression' or
                    parent.type == 'assignment_expression' and parent.child_by_field_name('left') == target)
                (writes if write else reads).add(key)
                if write and parent.type == 'assignment_expression':
                    right = parent.child_by_field_name('right')
                    if right is not None and right.type == 'call_expression':
                        callee = right.child_by_field_name('function')
                        if callee is not None and view.code(callee) in ('malloc', 'calloc', 'realloc', 'g_malloc', 'g_malloc0', 'g_new', 'g_new0'):
                            allocates.add(key)
            if indirect:
                limitations.append({'path': path, 'function': name, 'side': side, 'reason': '含间接调用；未进行函数指针目标或全程序别名证明'})
            scopes[ident] = {'id': ident, 'name': name, 'path': path, 'side': side,
                             'scope_type': 'function', 'body_ids': body_ids,
                             'start': start + 1, 'end': end, 'evidence': evidence,
                             'calls': calls, 'reads': sorted(reads), 'writes': sorted(writes),
                             'allocates': sorted(allocates), 'frees': sorted(frees),
                             'dependencies': sorted(visited),
                             'direct_refs': sorted(direct_refs),
                             'field_refs': sorted({view.code(n) for n in _walk(node) if n.type == 'field_identifier'}),
                             'layout_sensitive': any(n.type == 'sizeof_expression' for n in _walk(node)) or bool(direct_refs & {'offsetof', 'memcpy', 'memmove', 'memset'}),
                             'static': 'static' in view.code(node).split('{', 1)[0].split()}
            definitions[(side, name)].append(ident)
    functions = dict(scopes)
    scopes.update(changed_declarations)
    relations = []

    def connect(a, b, kind, detail):
        if a != b:
            relations.append({'left': a, 'right': b, 'kind': kind, 'detail': detail,
                              'reason': {'call': '直接调用的参数、返回值与调用约束需要联合核对',
                                         'state': '同一已解析状态存在跨函数读写，需要核对顺序和保护',
                                         'resource': '同一资源的分配与释放需要联合核对生命周期',
                                         'interface': '变更的声明或常量被函数使用，需要联合检查接口约束'}[kind]})

    for a, declaration in changed_declarations.items():
        for b, function in functions.items():
            shared = set(declaration['symbols']) & set(function['direct_refs'])
            if declaration['members'] and not (set(declaration['members']) & set(function['field_refs'])) and not function['layout_sensitive']:
                continue
            if declaration['side'] == function['side'] and shared:
                connect(a, b, 'interface', {'symbols': sorted(shared)})

    unresolved = set()
    for a, scope in scopes.items():
        for call in scope['calls']:
            targets = [i for i in definitions.get((scope['side'], call['name']), [])
                       if not scopes[i]['static'] or scopes[i]['path'] == scope['path']]
            same_file = [i for i in targets if scopes[i]['path'] == scope['path']]
            targets = same_file or targets
            if len(targets) == 1:
                connect(a, targets[0], 'call', dict(call))
            else:
                unresolved.add((scope['path'], call['name']))
    access = defaultdict(list)
    for ident, scope in scopes.items():
        for key in set(scope['reads'] + scope['writes'] + scope['allocates'] + scope['frees']):
            access[(scope['side'], key)].append(ident)
    # 对整个对象的确定性写入（例如 memset(&d->ext, ...)）会影响其子成员。
    # 普通成员读写仍按完整路径区分，不因共同容器形成关系。
    for (side, parent), members in list(access.items()):
        writers = [i for i in members if parent in scopes[i]['writes']]
        if not writers:
            continue
        for (child_side, child), users in list(access.items()):
            if child_side == side and child.startswith(parent + '.'):
                for writer in writers:
                    for reader in users:
                        connect(writer, reader, 'state', {'object': child, 'whole_object_write': parent})
    for (_, key), members in sorted(access.items()):
        for n, a in enumerate(members):
            for b in members[n + 1:]:
                sa, sb = scopes[a], scopes[b]
                if key in sa['allocates'] and key in sb['frees'] or key in sb['allocates'] and key in sa['frees']:
                    connect(a, b, 'resource', {'object': key})
                elif key in sa['writes'] or key in sb['writes']:
                    connect(a, b, 'state', {'object': key})
    if unresolved:
        limitations.append({'reason': '仅解析固定改动文件内的唯一直接调用；未读取的依赖或歧义符号不证明无关联',
                            'symbols': [{'path': p, 'name': n} for p, n in sorted(unresolved)]})
    if not snapshot.get('analysis_sources'):
        limitations.append({'reason': '没有可靠完整源码，无法证明跨函数关联；主审仍覆盖原始变更'})
    limitations.append({'reason': '状态关系仅覆盖可解析的直接对象成员及全局变量，未进行通用指针别名、宏展开或未读取文件的全程序分析'})
    return scopes, relations, limitations
