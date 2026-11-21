"""固定 Head/merge-base 的完整只读工作区；不运行或跟随仓库内容。"""
import hashlib
import contextlib
import fcntl
import json
import os
import re
import secrets
import shutil
import tarfile
import time
from collections import deque
from pathlib import Path, PurePosixPath

from .common import ReviewError, encode
from .diff import parse_diff, validate_patch
from .gitea import assert_version

WORKSPACE_ROOT = Path(os.environ.get('AI_REVIEW_WORKSPACE_ROOT', '/app/review-workspaces'))
CACHE_ROOT = WORKSPACE_ROOT / '.snapshot-cache'
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_FILES = 100000
RESERVE_BYTES = 256 * 1024 * 1024
LFS_HEADER = b'version https://git-lfs.github.com/spec/v1\n'


def _cache_limit():
    try:
        value = int(os.environ.get('AI_REVIEW_WORKSPACE_CACHE_BYTES', str(8 * 1024**3)))
    except ValueError:
        value = 8 * 1024**3
    return min(100 * 1024**3, max(1024**3, value))


def _digest_file(path, deadline_at=None, client=None):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            _deadline(deadline_at)
            if client is not None:
                client.check_active()
            value.update(chunk)
    return value.hexdigest()


def _prune_cache(keep=None, required=0):
    rows, total = [], 0
    for marker in CACHE_ROOT.glob('*/*/元数据.json'):
        try:
            data = json.loads(marker.read_text(encoding='utf-8'))
            archive = marker.parent / '源码归档.tar.gz'
            size = archive.stat().st_size
            rows.append((float(data.get('最后使用', 0)), marker.parent, size))
            total += size
        except (OSError, ValueError, TypeError):
            continue
    for _, folder, size in sorted(rows):
        if total + required <= _cache_limit():
            break
        if keep is not None and folder == keep:
            continue
        shutil.rmtree(folder, ignore_errors=True); total -= size
    if total + required > _cache_limit():
        raise ReviewError('固定源码归档缓存已达容量上限')


@contextlib.contextmanager
def cached_archive(client, sha, deadline_at=None):
    """按 Gitea 实例+仓库+提交固定不可变归档；锁持有到解包结束。"""
    if not re.fullmatch(r'[0-9a-f]{40}', sha or ''):
        raise ReviewError('固定源码提交标识无效')
    CACHE_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(CACHE_ROOT, 0o700)
    identity = hashlib.sha256(encode({'instance': client.url, 'repository': client.repo_id,
                                      'name': client.repo_name}).encode()).hexdigest()
    folder = CACHE_ROOT / identity / sha
    lock_path = CACHE_ROOT / '缓存互斥.lock'
    with lock_path.open('a') as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError:
                _deadline(deadline_at); client.check_active(); time.sleep(.05)
        archive, marker = folder / '源码归档.tar.gz', folder / '元数据.json'
        try:
            data = json.loads(marker.read_text(encoding='utf-8')) if marker.is_file() else {}
        except (OSError, ValueError):
            data = {}
        valid = (archive.is_file() and data.get('仓库身份') == identity and data.get('提交') == sha
                 and data.get('字节数') == archive.stat().st_size
                 and data.get('归档SHA256') == _digest_file(archive, deadline_at, client))
        if not valid:
            shutil.rmtree(folder, ignore_errors=True)
            _prune_cache(required=MAX_ARCHIVE_BYTES)
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = folder / ('下载中-' + secrets.token_hex(8))
            try:
                size = _download(client, client.prefix + '/archive/' + sha + '.tar.gz', temporary,
                                 MAX_ARCHIVE_BYTES, deadline_at)
                checksum = _digest_file(temporary, deadline_at, client)
                os.chmod(temporary, 0o444); os.replace(temporary, archive)
                data = {'仓库身份': identity, '提交': sha, '字节数': size,
                        '归档SHA256': checksum, '最后使用': time.time()}
                _write_json(marker, data)
            finally:
                temporary.unlink(missing_ok=True)
        else:
            data['最后使用'] = time.time(); _write_json(marker, data)
        try:
            yield archive
        finally:
            _prune_cache(keep=folder)
            fcntl.flock(lock, fcntl.LOCK_UN)


def _deadline(deadline_at):
    if deadline_at is not None and time.time() >= deadline_at:
        raise ReviewError('原生审查总期限已到，停止准备工作区')


def _download(client, path, destination, limit, deadline_at=None):
    client.check_active()
    _deadline(deadline_at)
    remaining = 120 if deadline_at is None else max(.1, min(120, deadline_at-time.time()))
    try:
        with client.session.get(client.url + '/api/v1' + path, timeout=(min(15, remaining), remaining),
                                allow_redirects=False, stream=True) as response:
            if response.status_code in (401, 403):
                raise ReviewError('读取固定版本归档的权限已失效')
            if response.status_code != 200:
                raise ReviewError(f'读取固定版本归档失败：HTTP {response.status_code}')
            size = 0
            with destination.open('xb') as output:
                for chunk in response.iter_content(1024 * 1024):
                    _deadline(deadline_at)
                    client.check_active()
                    size += len(chunk)
                    if size > limit:
                        raise ReviewError(f'固定版本资料超过 {limit} 字节上限')
                    if shutil.disk_usage(destination.parent).free < len(chunk) + RESERVE_BYTES:
                        raise ReviewError('原生审查工作区磁盘空间不足')
                    output.write(chunk)
            return size
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise ReviewError('固定版本资料写入失败') from exc
    except ReviewError:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise ReviewError('读取固定版本资料连接失败或中断') from exc


def _safe_name(name, prefix):
    if not isinstance(name, str) or '\x00' in name or '\\' in name:
        raise ReviewError('归档包含无效路径')
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ('', '.', '..') for part in path.parts):
        raise ReviewError('归档包含路径穿越或绝对路径')
    parts = path.parts[1:] if prefix and path.parts and path.parts[0] == prefix else path.parts
    if not parts:
        return None
    if parts[0] == '.git' or '.git' in parts:
        raise ReviewError('归档不应包含 Git 管理目录')
    return Path(*parts)


def safe_extract(archive_path, destination, deadline_at=None, check_active=None):
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    entries, unresolved, total, seen = [], [], 0, set()
    try:
        with tarfile.open(archive_path, mode='r:*') as archive:
            members, declared_total = [], 0
            for member in archive:
                _deadline(deadline_at)
                if check_active:
                    check_active()
                if len(members) >= MAX_FILES:
                    raise ReviewError(f'归档文件数超过 {MAX_FILES}')
                if member.isfile():
                    if member.size < 0 or member.size > MAX_FILE_BYTES:
                        raise ReviewError(f'归档单文件超过 {MAX_FILE_BYTES} 字节：{member.name}')
                    declared_total += member.size
                    if declared_total > MAX_EXTRACTED_BYTES:
                        raise ReviewError('归档解包声明大小超过安全上限')
                members.append(member)
            roots = {PurePosixPath(m.name).parts[0] for m in members if PurePosixPath(m.name).parts}
            prefix = next(iter(roots)) if len(roots) == 1 else None
            for member in members:
                _deadline(deadline_at)
                if check_active:
                    check_active()
                relative = _safe_name(member.name, prefix)
                if relative is None:
                    continue
                if member.issym() or member.islnk():
                    unresolved.append({'路径': relative.as_posix(), '类型': '符号链接或硬链接',
                                       '原因': '安全工作区不提取、不跟随链接'})
                    continue
                if member.isdev() or member.isfifo():
                    raise ReviewError(f'归档包含不允许的设备或管道：{relative}')
                if not (member.isdir() or member.isfile()):
                    raise ReviewError(f'归档包含不支持的成员：{relative}')
                key = relative.as_posix()
                if key in seen:
                    raise ReviewError(f'归档包含重复路径：{key}')
                seen.add(key)
                target = destination / relative
                if member.isdir():
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                    continue
                if member.size < 0 or member.size > MAX_FILE_BYTES:
                    raise ReviewError(f'归档单文件超过 {MAX_FILE_BYTES} 字节：{key}')
                total += member.size
                if total > MAX_EXTRACTED_BYTES or shutil.disk_usage(destination).free < member.size + RESERVE_BYTES:
                    raise ReviewError('归档解包大小或磁盘空间超过安全上限')
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ReviewError(f'归档文件无法读取：{key}')
                digest = hashlib.sha256()
                first = b''
                with target.open('xb') as output:
                    remaining = member.size
                    while remaining:
                        _deadline(deadline_at)
                        if check_active:
                            check_active()
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ReviewError(f'归档文件提前结束：{key}')
                        if not first:
                            first = chunk[:200]
                        output.write(chunk); digest.update(chunk); remaining -= len(chunk)
                    if source.read(1):
                        raise ReviewError(f'归档文件尺寸与声明不一致：{key}')
                os.chmod(target, 0o444)
                is_lfs = first.startswith(LFS_HEADER)
                if is_lfs:
                    unresolved.append({'路径': key, '类型': 'Git LFS 指针',
                                       '原因': '归档未包含 LFS 真实对象，不把指针当作源码'})
                entries.append({'路径': key, '字节数': member.size, 'SHA256': digest.hexdigest(),
                                'Git LFS 指针': is_lfs})
        modules = destination / '.gitmodules'
        if modules.is_file():
            try:
                text = modules.read_text(encoding='utf-8')
                for path in re.findall(r'^\s*path\s*=\s*(.+?)\s*$', text, re.M):
                    relative = _safe_name(path, None)
                    if relative:
                        unresolved.append({'路径': relative.as_posix(), '类型': 'Git 子模块',
                                           '原因': '固定归档不自动获取外部子模块'})
            except (OSError, UnicodeError):
                unresolved.append({'路径': '.gitmodules', '类型': 'Git 子模块声明',
                                   '原因': '无法安全解析子模块路径'})
        for root, folders, _ in os.walk(destination, topdown=False):
            for folder in folders:
                os.chmod(Path(root) / folder, 0o555)
        os.chmod(destination, 0o555)
        return {'文件': sorted(entries, key=lambda row: row['路径']),
                '未决内容': sorted(unresolved, key=lambda row: (row['路径'], row['类型']))}
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _write_json(path, value):
    temporary = path.with_name(path.name + '.写入中')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(temporary, 0o444)
    os.replace(temporary, path)


def prepare(client, job, snapshot, deadline_at=None):
    if not re.fullmatch(r'[0-9a-f]{32}', job['id']):
        raise ReviewError('任务标识无法建立安全工作区')
    WORKSPACE_ROOT.mkdir(mode=0o755, parents=True, exist_ok=True)
    os.chmod(WORKSPACE_ROOT, 0o755)
    root = WORKSPACE_ROOT / job['id']
    marker = root / '准备完成.json'
    if marker.exists():
        data = json.loads(marker.read_text(encoding='utf-8'))
        if (data.get('快照标识') != snapshot['id'] or data.get('仓库标识') != snapshot.get('repository')
                or data.get('任务仓库配置') != job['repository']):
            raise ReviewError('已有工作区与本任务固定快照不一致，未覆盖')
        return str(root), data
    if root.exists():
        if root.is_symlink() or root.resolve().parent != WORKSPACE_ROOT.resolve():
            raise ReviewError('未完成工作区路径无法安全回收')
        shutil.rmtree(root)
    root.mkdir(mode=0o755, exist_ok=False)
    try:
        _deadline(deadline_at)
        manifests = {}
        for label, sha in (('before', snapshot['merge_base']), ('after', snapshot['head'])):
            with cached_archive(client, sha, deadline_at) as archive:
                manifests[label] = safe_extract(archive, root / label, deadline_at, client.check_active)
        diff = root / '变更.diff'
        _download(client, client.prefix + f'/pulls/{job["number"]}.diff', diff, 256 * 1024 * 1024, deadline_at)
        raw = diff.read_bytes()
        patches = parse_diff(raw)
        changed = sorted({patch.new_path or patch.old_path for patch in patches if patch.new_path or patch.old_path})
        os.chmod(diff, 0o444)
        manifest = {'快照标识': snapshot['id'], 'Head': snapshot['head'], '比较基点': snapshot['merge_base'],
                    '变更文件': changed, '修改前源码': manifests['before'], '修改后源码': manifests['after']}
        _write_json(root / '源码清单.json', manifest)
        marker_data = {'快照标识': snapshot['id'], '仓库标识': snapshot.get('repository'),
                       '任务仓库配置': job['repository'], 'Head': snapshot['head'], '比较基点': snapshot['merge_base'],
                       '变更文件数': len(changed), '工作区状态': '已固定且源码只读'}
        _write_json(marker, marker_data)
        assert_version(client, job['number'], snapshot)
        return str(root), marker_data
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _read_fixed(path, deadline_at, store, job):
    """从已安全解包的普通文件分块读取，总期限和租约失效都会中断。"""
    if path is None:
        return b''
    if not path.is_file() or path.is_symlink():
        raise ReviewError('固定版本文件不可用或不是普通文件')
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ReviewError(f'固定版本单文件超过 {MAX_FILE_BYTES} 字节')
    chunks = []
    with path.open('rb') as stream:
        while True:
            _deadline(deadline_at); store.active(job['id'], job['owner'])
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    return b''.join(chunks)


def gitea_legacy_inline_positions(patch):
    """Gitea 1.26.4 默认把锚点及前三个补丁记录写入行评回执。"""
    unsafe = set()
    for hunk in patch.hunks:
        recent = deque(maxlen=4)
        old = hunk.old_start - bool(hunk.old_count)
        new = hunk.new_start - bool(hunk.new_count)
        for kind, raw in hunk.rows:
            old += kind != '+'
            new += kind != '-'
            try:
                raw.decode('utf-8')
            except UnicodeDecodeError:
                recent.append(False)
            else:
                recent.append(True)
            if kind in ('+', '-') and not all(recent):
                unsafe.add(('new', new) if kind == '+' else ('old', old))
    return unsafe


def bind_snapshot_evidence(store, job, snapshot, workspace, deadline_at=None):
    """
    用原生工作区的固定 Diff 及前后源码生成行内证据。

    旧 capture 的 C/C++ AST 筛选仅服务历史单元引擎；原生会话已可读取
    所有普通文本文件，因此行内定位也必须从同一不可变工作区核对。
    """
    with contextlib.ExitStack() as cleanup:
        return _bind_snapshot_evidence(store, job, snapshot, workspace, deadline_at, cleanup)


def _bind_snapshot_evidence(store, job, snapshot, workspace, deadline_at, cleanup):
    from .snapshot import SourceFile

    root = Path(workspace)
    diff_path = root / 'change.diff' if (root / 'change.diff').exists() else root / '变更.diff'
    git_layout = snapshot.get('workspace_layout') in ('git-worktree-v1', 'git-worktree-v2')
    byte_protocol = snapshot.get('pr_agent_protocol')
    from .pr_agent_encoding import PROTOCOL, UnreviewableSource, uses_local_git
    partial = byte_protocol == PROTOCOL
    git_inventory = {}
    if byte_protocol:
        from .pr_agent_encoding import blocks, decode_blob, render_block, unpack
        raw = unpack(snapshot)
        patch_blocks = blocks(raw)
        if len(patch_blocks) != snapshot['coverage']['files_total']:
            raise ReviewError('固定补丁文件结构与快照不一致')
        records = [(parse_diff(block)[0], block) for block in patch_blocks]
        if partial:
            from .git_cache import ObjectReader, run_git
            pair = ['--no-ext-diff', '--no-textconv', '-M', snapshot['merge_base'], snapshot['head'], '--']
            check = lambda: store.active(job['id'], job['owner'])
            metadata = iter(run_git(['diff', '--raw', '--no-abbrev', '-z', *pair],
                                    cwd=root, check=check).split(b'\0')[:-1])
            identities = []
            for entry in metadata:
                _, _, old_blob, new_blob, status = entry.split()
                path = next(metadata)
                if status[:1] in (b'R', b'C'):
                    path = next(metadata)
                identities.append((path.decode('utf-8'), old_blob.decode('ascii'), new_blob.decode('ascii')))
            native_blocks = blocks(run_git(['diff', *pair], cwd=root, check=check))
            git_inventory = {block.split(b'\n', 1)[0]: identity
                             for block, identity in zip(native_blocks, identities, strict=True)}
            if len(git_inventory) != len(patch_blocks) or set(git_inventory) != {
                    block.split(b'\n', 1)[0] for block in patch_blocks}:
                raise ReviewError('Gitea 补丁文件范围与固定 Git 提交不一致')
            # 大 PR 共用一个 cat-file 进程，避免每侧源码启动一次 Git。
            reader = ObjectReader(root, check)
            cleanup.callback(reader.close)
    else:
        raw = snapshot['diff'].encode() if git_layout else _read_fixed(diff_path, deadline_at, store, job)
        records = [(patch, None) for patch in parse_diff(raw)]
    rendered = []
    files, anchors, skipped = [], {}, []
    with store.tx() as c:
        c.execute('CREATE TABLE IF NOT EXISTS review_snapshot_sources '
                  '(job TEXT,path TEXT,side TEXT,code TEXT,PRIMARY KEY(job,path,side))')
    for patch, block in records:
        path = patch.new_path or patch.old_path
        if partial:
            fixed_path, old_blob, new_blob = git_inventory[block.split(b'\n', 1)[0]]
            if path and path != fixed_path:
                raise ReviewError('Gitea 补丁路径与固定 Git 提交不一致')
            path = fixed_path
        if not path and block:
            path = block.split(b'\n', 1)[0].decode('utf-8', 'backslashreplace')[:1000]
        try:
            _deadline(deadline_at); store.active(job['id'], job['owner'])
            if patch.unsupported:
                if partial and patch.unsupported == '二进制文件、符号链接或子模块不产生行内意见':
                    raise UnreviewableSource(patch.unsupported)
                raise ReviewError(patch.unsupported)
            if not path or not patch.hunks:
                skipped.append({'path': path or '非文本变更',
                                'reason': '纯重命名、模式变更或无文本修改，不做行内定位',
                                'evidence_gap': bool(byte_protocol),
                                **({'kind': 'metadata_only'} if partial else {})})
                continue
            old_path = root / 'before' / patch.old_path if patch.old_path else None
            new_path = root / 'after' / patch.new_path if patch.new_path else None
            if partial:
                old = reader.read(old_blob) if patch.old_path else b''
                new = reader.read(new_blob) if patch.new_path else b''
            elif git_layout:
                from .git_workspace import read_blob
                old = read_blob(root, snapshot['merge_base'], patch.old_path) if patch.old_path else b''
                new = read_blob(root, snapshot['head'], patch.new_path) if patch.new_path else b''
            else:
                old = _read_fixed(old_path, deadline_at, store, job)
                new = _read_fixed(new_path, deadline_at, store, job)
            encodings = None
            if byte_protocol:
                rules = snapshot['source_encodings']
                old_text, old_codec = decode_blob(old, patch.old_path or path, '旧侧', rules, reject_mixed=partial)
                new_text, new_codec = decode_blob(new, patch.new_path or path, '新侧', rules, reject_mixed=partial)
                encodings = {'old': old_codec, 'new': new_codec}
            try:
                found = validate_patch(patch, old, new, snapshot['id'], encodings)
            except ReviewError as exc:
                # 同一固定 SHA 的归档源码无法被固定 Diff 还原，说明
                # 版本身份或证据已不一致，不能降级为普通覆盖缺口后继续发布。
                if str(exc) not in ('文件包含二进制内容', '源码不是 UTF-8，不能精确引用'):
                    exc.fatal_job = True
                raise
            if byte_protocol:
                normalized = render_block(block, patch, encodings)
                if uses_local_git(byte_protocol) and set(encodings.values()) != {'utf-8'}:
                    unsafe = gitea_legacy_inline_positions(patch)
                    for anchor in found:
                        if (anchor['side'], anchor['line']) in unsafe:
                            anchor['gitea_legacy_inline'] = False
            else:
                old_text, new_text = old.decode('utf-8'), new.decode('utf-8')
            needed = len(old) + len(new)
            if shutil.disk_usage(store.path.parent).free < needed * 3 + 64 * 1024 * 1024:
                raise ReviewError('源码证据存储空间不足：需保留本文件三倍写入空间及 64 MiB 收尾空间')
            with store.tx() as c:
                store._active(c, job['id'], job['owner'])
                c.executemany('INSERT OR REPLACE INTO review_snapshot_sources VALUES (?,?,?,?)',
                              [(job['id'], path, 'old', old_text),
                               (job['id'], path, 'new', new_text)])
            reference = {'path': path, 'old_path': patch.old_path, 'new_path': patch.new_path,
                         'old': None, 'new': None, 'source_job': job['id'],
                         'anchors': [item['id'] for item in found],
                         'patch': [{'old_start': h.old_start, 'new_start': h.new_start,
                                    'old_count': h.old_count, 'new_count': h.new_count,
                                    'kinds': [kind for kind, _ in h.rows]} for h in patch.hunks]}
            files.append(SourceFile(reference, store))
            anchors.update((item['id'], item) for item in found)
            if byte_protocol:
                rendered.append(normalized)
        except (ReviewError, UnicodeError) as exc:
            from .common import Stale
            if isinstance(exc, Stale) or getattr(exc, 'fatal_job', False):
                raise
            if partial and not isinstance(exc, UnreviewableSource):
                raise
            skipped.append({'path': path or '非文本变更',
                            'reason': str(exc) if isinstance(exc, ReviewError) else '源码不是 UTF-8',
                            'evidence_gap': True,
                            **({'kind': 'unsupported' if patch.unsupported else 'encoding'} if partial else {})})
    snapshot['files'], snapshot['anchors'] = files, anchors
    if byte_protocol:
        if not skipped and not partial:
            from .git_cache import run_git
            paths = run_git(['diff', '--name-only', '--no-renames', '--no-ext-diff', '--no-textconv', '-z',
                             snapshot['merge_base'], snapshot['head'], '--'], cwd=root,
                            check=lambda: store.active(job['id'], job['owner']))
            expected = set(paths.split(b'\0')) - {b''}
            actual = set()
            for patch, block in records:
                if patch.old_path and b'\ncopy from ' not in block:
                    actual.add(patch.old_path.encode())
                if patch.new_path:
                    actual.add(patch.new_path.encode())
            for path in sorted(expected ^ actual):
                skipped.append({'path': path.decode('utf-8', 'backslashreplace'),
                                'reason': 'Gitea 补丁文件范围与固定 Git 提交不一致',
                                'evidence_gap': True})
        snapshot['diff'] = ''.join(rendered)
    snapshot['coverage'] = {'files_total': len(records), 'files_reviewed': len(files),
                            'skipped': skipped,
                            'complete': not any(row.get('evidence_gap') for row in skipped)}
    return snapshot


def cleanup(job_id):
    if not re.fullmatch(r'[0-9a-f]{32}', job_id):
        return
    root = WORKSPACE_ROOT / job_id
    if root.exists() and not root.is_symlink() and root.resolve().parent == WORKSPACE_ROOT.resolve():
        shutil.rmtree(root, ignore_errors=True)
