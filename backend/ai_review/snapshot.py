"""固定提交上的源码证据与函数上下文；不运行仓库程序或构建脚本。"""
import posixpath
import contextlib
import shutil
import re
from .common import ReviewError, digest
from .diff import parse_diff, validate_patch
from .gitea import version_of, assert_version

SOURCE_SUFFIXES = ('.c', '.h', '.cc', '.cpp', '.hpp', '.cxx', '.hxx')
ANALYSIS_VERSION = 4


class SourceFile(dict):
    """快照仅保存引用；逐文件读取已经固定在数据库中的源码。"""
    def __init__(self, value, store):
        super().__init__(value); self.store = store

    def __getitem__(self, key):
        if key in ('old','new') and dict.get(self,'source_job'):
            with contextlib.closing(self.store.connect()) as c:
                row=c.execute('SELECT code FROM review_snapshot_sources WHERE job=? AND path=? AND side=?',
                              (dict.__getitem__(self,'source_job'),dict.__getitem__(self,'path'),key)).fetchone()
            if row is None:raise ReviewError('固定快照源码缺失，未使用当前仓库代码替代')
            return row[0]
        return dict.__getitem__(self,key)

    def get(self,key,default=None):
        return self[key] if key in self else default


def hydrate(snapshot, store):
    if snapshot:
        snapshot['files']=[SourceFile(f,store) if f.get('source_job') else f for f in snapshot.get('files',[])]
    return snapshot


def capture(client, number, config, store=None, job=None):
    client.max_file_bytes = 32*1024*1024
    server_version=client.version()
    client.repository()
    version = version_of(client.pr(number))
    identity = dict(version, repository=client.repo_id, number=number, config_revision=config.get('execution_revision', config['revision']),server_version=server_version, analysis_version=ANALYSIS_VERSION)
    snapshot = dict(version, repository=client.repo_id, plan_complete=False,
                    analysis_version=ANALYSIS_VERSION, server_version=server_version,
                    id=digest(identity), files=[], anchors={},
                    coverage={'files_total': 0, 'files_reviewed': 0, 'skipped': []})
    patches = client.diff_patches(number) if hasattr(type(client),'diff_patches') else parse_diff(client.request('GET', client.prefix + f'/pulls/{number}.diff', raw=True, limit=64*1024*1024))
    if store:
        with store.tx() as c:
            c.execute('CREATE TABLE IF NOT EXISTS review_snapshot_sources (job TEXT,path TEXT,side TEXT,code TEXT,PRIMARY KEY(job,path,side))')
    total = 0
    seen_paths=set()
    for patch in patches:
        snapshot['coverage']['files_total'] += 1
        path = patch.new_path or patch.old_path
        if path and path in seen_paths:raise ReviewError('补丁包含重复文件，停止错误归属判断')
        if path:seen_paths.add(path)
        try:
            if patch.unsupported:
                raise ReviewError(patch.unsupported)
            if not path or not patch.hunks:
                raise ReviewError('纯重命名、模式变更或无文本修改，不做行内分析')
            if not path.lower().endswith(SOURCE_SUFFIXES):
                raise ReviewError('第一期重点支持 C/C++ 源码，该文件尚未覆盖')
            old = client.file(patch.old_path, version['merge_base'])
            new = client.file(patch.new_path, version['head'])
            anchors = validate_patch(patch, old, new, snapshot['id'])
            old_text, new_text = old.decode('utf-8'), new.decode('utf-8')
            total += len(old) + len(new)
            snapshot['files'].append({'path': path, 'old_path': patch.old_path, 'new_path': patch.new_path,
                                      'old': old_text, 'new': new_text, 'anchors': [a['id'] for a in anchors],
                                      'patch':[{'old_start':h.old_start,'new_start':h.new_start,
                                                'old_count':h.old_count,'new_count':h.new_count,
                                                'kinds':[k for k,_ in h.rows]} for h in patch.hunks]})
            if store:
                if shutil.disk_usage(store.path.parent).free < (len(old)+len(new))*3+64*1024*1024:
                    snapshot['files'].pop()
                    raise ReviewError('源码证据存储空间不足：需保留本文件三倍写入空间及 64 MiB 收尾空间')
                with store.tx() as c:
                    store._active(c,job['id'],job['owner'])
                    c.executemany('INSERT OR IGNORE INTO review_snapshot_sources VALUES (?,?,?,?)',
                                  [(job['id'],path,'old',old_text),(job['id'],path,'new',new_text)])
                reference=dict(snapshot['files'][-1],old=None,new=None,source_job=job['id'])
                snapshot['files'][-1]=SourceFile(reference,store)
            snapshot['anchors'].update((a['id'], a) for a in anchors)
            if store and len(snapshot['files']) % config['max_files'] == 0:
                store.update(job['id'],job['owner'],snapshot=snapshot,batch_started=__import__('time').time(),stage=f'已固定 {len(snapshot["files"])} 个文件的源码证据')
        except (ReviewError, UnicodeError) as exc:
            from .common import Stale
            if isinstance(exc, Stale) or getattr(exc,'fatal_job',False):
                raise
            snapshot['coverage']['skipped'].append({'path': path or '非文本变更', 'reason': str(exc) if isinstance(exc, ReviewError) else '源码不是 UTF-8'})
    assert_version(client, number, version)
    snapshot['coverage']['files_reviewed'] = len(snapshot['files'])
    snapshot['coverage']['complete'] = not snapshot['coverage']['skipped']
    return snapshot


def parse_source(text, path, language=None):
    import tree_sitter
    if language == 'c' or language is None and path.lower().endswith(('.c', '.h')):
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


def work_items(snapshot, client=None, on_file=None):
    """固定 before/after 的分析单元；同一函数的新旧修改只进入一次模型。"""
    if snapshot.get('analysis_version', 3) >= 4:
        from .syntax_context import build_items
    else:
        from .context import build_items
    return build_items(snapshot, client, on_file=on_file) if on_file and snapshot.get('analysis_version',3)>=4 else build_items(snapshot, client)
