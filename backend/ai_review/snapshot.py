"""固定提交上的源码证据与函数上下文；不运行仓库程序或构建脚本。"""
import posixpath
import re
from .common import ReviewError, digest
from .diff import parse_diff, validate_patch
from .gitea import version_of, assert_version

SOURCE_SUFFIXES = ('.c', '.h', '.cc', '.cpp', '.hpp', '.cxx', '.hxx')
ANALYSIS_VERSION = 3


def capture(client, number, config):
    server_version=client.version()
    client.repository()
    version = version_of(client.pr(number))
    identity = dict(version, repository=client.repo_id, number=number, config_revision=config['revision'],server_version=server_version, analysis_version=ANALYSIS_VERSION)
    snapshot = dict(version, analysis_version=ANALYSIS_VERSION, server_version=server_version,id=digest(identity), files=[], anchors={}, coverage={'files_total': 0, 'files_reviewed': 0, 'skipped': []})
    patches = parse_diff(client.request('GET', client.prefix + f'/pulls/{number}.diff', raw=True, limit=2*1024*1024))
    snapshot['coverage']['files_total'] = len(patches)
    total = 0
    for patch in patches:
        path = patch.new_path or patch.old_path
        try:
            if not path or not patch.hunks:
                raise ReviewError('纯重命名、模式变更或无文本修改，不做行内分析')
            if patch.unsupported:
                raise ReviewError(patch.unsupported)
            if not path.lower().endswith(SOURCE_SUFFIXES):
                raise ReviewError('第一期重点支持 C/C++ 源码，该文件尚未覆盖')
            if len(snapshot['files']) >= config['max_files']:
                raise ReviewError('超过本次文件预算')
            changed_lines = sum(kind in ('+', '-') for h in patch.hunks for kind, _ in h.rows)
            if changed_lines > 5000 or len(snapshot['anchors']) + changed_lines > 20000:
                raise ReviewError('变更行超过位置证据预算，未创建过量锚点')
            old = client.file(patch.old_path, version['merge_base'])
            new = client.file(patch.new_path, version['head'])
            if total + len(old) + len(new) > 2*1024*1024:
                raise ReviewError('超过本次源码证据预算')
            anchors = validate_patch(patch, old, new, snapshot['id'])
            old_text, new_text = old.decode('utf-8'), new.decode('utf-8')
            total += len(old) + len(new)
            snapshot['files'].append({'path': path, 'old_path': patch.old_path, 'new_path': patch.new_path,
                                      'old': old_text, 'new': new_text, 'anchors': [a['id'] for a in anchors]})
            snapshot['anchors'].update((a['id'], a) for a in anchors)
        except (ReviewError, UnicodeError) as exc:
            from .common import Stale
            if isinstance(exc, Stale):
                raise
            snapshot['coverage']['skipped'].append({'path': path or '非文本变更', 'reason': str(exc) if isinstance(exc, ReviewError) else '源码不是 UTF-8'})
    assert_version(client, number, version)
    snapshot['coverage']['files_reviewed'] = len(snapshot['files'])
    snapshot['coverage']['complete'] = not snapshot['coverage']['skipped']
    return snapshot


def parse_source(text, path):
    import tree_sitter
    if path.lower().endswith(('.c', '.h')):
        import tree_sitter_c
        language = tree_sitter.Language(tree_sitter_c.language())
    else:
        import tree_sitter_cpp
        language = tree_sitter.Language(tree_sitter_cpp.language())
    parser = tree_sitter.Parser(language)
    raw = text.encode()
    tree = parser.parse(raw)
    return raw, tree


def comment_only_lines(text, path):
    raw, tree = parse_source(text, path)
    masked = bytearray(raw)
    def visit(node):
        if node.type == 'comment':
            for i in range(node.start_byte,node.end_byte):
                if masked[i] not in (10,13):masked[i]=32
            return
        for child in node.named_children:visit(child)
    visit(tree.root_node)
    return {i for i,line in enumerate(bytes(masked).split(b'\n'),1) if not line.strip()}


def functions(text, path):
    raw, tree = parse_source(text, path)
    result = []
    def visit(node):
        if node.type == 'function_definition':
            declarator = node.child_by_field_name('declarator')
            # 用完整声明区分重载、同名方法和条件编译分支。
            label = raw[declarator.start_byte:declarator.end_byte].decode() if declarator else '匿名函数'
            parents, parent = [], node.parent
            while parent is not None:
                if parent.type in ('namespace_definition','class_specifier','struct_specifier'):
                    name = parent.child_by_field_name('name')
                    if name is not None:
                        parents.append(raw[name.start_byte:name.end_byte].decode())
                parent = parent.parent
            if parents:
                label = '::'.join(reversed(parents)) + '::' + label
            result.append({'start': node.start_point.row+1, 'end': node.end_point.row+1,
                           'name': label, 'code': raw[node.start_byte:node.end_byte].decode(), 'parse_error': node.has_error})
            return
        for child in node.named_children:
            visit(child)
    visit(tree.root_node)
    return result, tree.root_node.has_error


def work_items(snapshot, client=None):
    """固定 before/after 的分析单元；同一函数的新旧修改只进入一次模型。"""
    from .context import build_items
    return build_items(snapshot, client)
