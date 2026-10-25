"""固定修订转换与逐提交验证，基于 b8232258ee 的单仓库工具整理。"""
import contextlib
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from urllib.parse import quote, unquote
import xml.etree.ElementTree as ET

from .common import MigrationError, Cancelled, require, source_url, within_source, digest, atomic_json


class Process:
    """输出写入磁盘，短周期检查取消和空间；不在内存累计完整转换日志。"""
    def __init__(self, folder, tick=lambda: None, event=lambda text: None, secrets=()):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.tick, self.event, self.secrets = tick, event, secrets
        self.counter = 0

    def run_file(self, argv, *, cwd=None, env=None, data=None, timeout=86400):
        self.tick()
        require(shutil.disk_usage(self.folder).free > 32 * 1024 * 1024, '工作盘可用空间不足，已停止', 'disk')
        self.counter += 1
        output = self.folder / f'command-{self.counter}-{time.time_ns()}.bin'
        started, last_event = time.monotonic(), time.monotonic()
        tail = b''
        environment = {k: v for k, v in os.environ.items() if not k.startswith(('GIT_', 'SVN_'))}
        environment.update(LANG='C.UTF-8', LC_ALL='C.UTF-8', GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
        environment.update(env or {})
        proc = None
        tag = uuid.uuid4().hex
        marker = self.folder / ('.process-' + tag + '.json')
        environment['GM_SVN_PROCESS_TAG'] = tag
        try:
            with output.open('wb') as out, tempfile.TemporaryFile() as input_file:
                if data:
                    input_file.write(data)
                input_file.seek(0)
                proc = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=input_file,
                                        stdout=out, stderr=subprocess.PIPE, start_new_session=True)
                atomic_json(marker, {'进程': proc.pid, '标识': tag})
                with selectors.DefaultSelector() as selector:
                    selector.register(proc.stderr, selectors.EVENT_READ)
                    while proc.poll() is None:
                        for key, _ in selector.select(0.3):
                            block = os.read(key.fd, 65536)
                            if block:
                                tail = (tail + block)[-8192:]
                            else:
                                selector.unregister(key.fileobj)
                        self.tick()
                        if time.monotonic() - started > timeout:
                            raise MigrationError('子步骤超过执行时限，现场已保留', 'timeout')
                        if time.monotonic() - last_event > 10:
                            require(shutil.disk_usage(self.folder).free > 32 * 1024 * 1024, '工作盘可用空间不足，已停止', 'disk')
                            self.event('子步骤仍在运行，已用时 ' + str(int(time.monotonic() - started)) + ' 秒')
                            last_event = time.monotonic()
                    tail = (tail + proc.stderr.read())[-8192:]
                if proc.returncode:
                    error = tail.decode('utf-8', errors='replace')
                    for secret in self.secrets:
                        if secret:
                            error = error.replace(secret, '[凭据已隐藏]')
                    raise MigrationError('命令执行失败：' + error[-4000:], 'command')
            return output
        except BaseException:
            if proc is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            if output.exists():
                output.replace(self.folder / ('interrupted-' + tag + '.log'))
            raise
        finally:
            if proc and proc.stderr:
                proc.stderr.close()
            marker.unlink(missing_ok=True)

    def run(self, argv, **kwargs):
        path = self.run_file(argv, **kwargs)
        try:
            require(path.stat().st_size <= 32 * 1024 * 1024, '命令输出超过内存读取限额', 'capacity')
            return path.read_bytes()
        finally:
            path.unlink(missing_ok=True)


class Source:
    def __init__(self, record, process, credential_dir):
        self.record, self.process = record, process
        self.options = ['--non-interactive', '--no-auth-cache', '--username', record['username']]
        self.password = record.get('password', '')
        if self.password:
            self.options += ['--password-from-stdin']
        self.config = Path(credential_dir) / ('svn-' + str(record['id']))
        self.config.mkdir(mode=0o700, exist_ok=True)
        self.options += ['--config-dir', str(self.config)]
        self.clone_env = {}
        if record.get('ca_pem'):
            ca = self.config / 'ca.pem'
            ca.write_text(record['ca_pem']); ca.chmod(0o600)
            (self.config / 'servers').write_text('[global]\nssl-authority-files = ' + str(ca) + '\nssl-trust-default-ca = yes\n')
        if self.password:
            password_file = self.config / 'password'
            password_file.write_text(self.password); password_file.chmod(0o600)
            askpass = self.config / 'askpass.py'
            askpass.write_text('#!/usr/bin/env python3\nimport os,sys\nfrom pathlib import Path\n'
                              'expected="Password for \\\""+os.environ["SVN_MIGRATION_USER"]+"\\\": "\n'
                              'if len(sys.argv)!=2 or not sys.argv[1].lower().startswith("password"):\n sys.exit(1)\n'
                              'print(Path(os.environ["SVN_MIGRATION_PASSWORD_FILE"]).read_text())\n')
            askpass.chmod(0o700)
            self.clone_env.update(GIT_ASKPASS=str(askpass), SVN_MIGRATION_USER=record['username'], SVN_MIGRATION_PASSWORD_FILE=str(password_file))

    def file(self, *args, timeout=86400):
        return self.process.run_file(['svn', *map(str, args), *self.options],
                                     data=(self.password + '\n').encode() if self.password else None, timeout=timeout)

    def xml(self, *args, timeout=86400):
        file = self.file(*args, timeout=timeout)
        try:
            return ET.parse(file).getroot()
        except ET.ParseError:
            raise MigrationError('SVN 返回了无效的结构化数据', 'source') from None
        finally:
            file.unlink(missing_ok=True)

    def info(self, url, revision='HEAD'):
        entry = self.xml('info', '--xml', '-r', revision, url + '@' + str(revision), timeout=120).find('entry')
        require(entry is not None and entry.get('kind') == 'dir', 'SVN 地址不是可读取的目录', 'source')
        return {'uuid': entry.findtext('repository/uuid'), 'root': entry.findtext('repository/root'),
                'url': entry.findtext('url'), 'revision': int(entry.get('revision')),
                'path': unquote(entry.findtext('relative-url')[1:])}

    def properties(self, url, revision, recursive=True):
        args = ['proplist', '--xml', '--verbose', '-r', str(revision)]
        if recursive:
            args.append('--recursive')
        root = self.xml(*args, url + '@' + str(revision))
        result = {}
        for target in root.findall('target'):
            result[source_url(target.get('path'))] = {p.get('name'): p.text or '' for p in target.findall('property')}
        return result

    def inventory(self, url, layout):
        info = self.info(url)
        # 用仓库根 HEAD 检查停写状态，不能只使用项目目录最后修改号。
        info['head_revision'] = self.info(info['root'])['revision']
        revision = info['head_revision']
        info['revision'] = revision
        if layout.get('project'):
            # 纳管边界固定为整个项目。仅在入口同层布局明确时映射原生分支，
            # 任意命名或多项目混放保持完整目录树，不能猜测并遗漏内容。
            from .layout_detection import NAMES
            entries = self.xml('list', '--xml', '-r', revision, url + '@' + str(revision)).findall('list/entry')
            roles = {key: [e.findtext('name') for e in entries if e.get('kind') == 'dir' and e.findtext('name').lower() in names]
                     for key, names in NAMES.items()}
            chosen = {'trunk': '.', 'branches': '', 'tags': '', 'default_branch': layout['default_branch'], 'project': True}
            if len(roles['trunk']) == 1 and all(len(roles[k]) <= 1 for k in ('branches', 'tags')):
                chosen.update({k: rows[0] if rows else '' for k, rows in roles.items()})
                for key in ('branches', 'tags'):
                    if chosen[key]:
                        container = url + '/' + quote(chosen[key], safe='/')
                        if any(e.get('kind') != 'dir' for e in self.xml('list', '--xml', '-r', revision, container + '@' + str(revision)).findall('list/entry')):
                            chosen.update(trunk='.', branches='', tags=''); break
            layout = chosen
        branches, tags = [], []
        for key, rows in (('trunk', None), ('branches', branches), ('tags', tags)):
            if not layout[key]:
                continue
            path = url if layout[key] == '.' else url + '/' + quote(layout[key], safe='/')
            self.info(path, revision)
            if rows is not None:
                entries = self.xml('list', '--xml', '-r', revision, path + '@' + str(revision)).findall('list/entry')
                require(all(e.get('kind') == 'dir' for e in entries), '分支或标签容器中存在直接文件，请调整布局', 'layout')
                rows.extend(e.findtext('name') for e in entries)
        refs = ['refs/heads/' + layout['default_branch']]
        refs += ['refs/heads/' + n for n in branches] + ['refs/tags/' + n for n in tags]
        if layout.get('project') and layout['trunk'] != '.':
            name = 'svn-project'
            while any(r == 'refs/heads/' + name or r.startswith('refs/heads/' + name + '/') or ('refs/heads/' + name).startswith(r + '/') for r in refs):
                name += '-full'
            info['project_ref'] = 'refs/heads/' + name
            refs.append(info['project_ref'])
        require(len(refs) == len(set(refs)) and not {'trunk', 'tags'}.intersection(branches) and not any(n.startswith('svn-archive/') for n in branches), '分支命名与默认分支或保留命名冲突', 'layout')
        for ref in refs:
            self.process.run(['git', 'check-ref-format', ref], timeout=30)
        properties = self.properties(url, revision)
        all_entries = self.xml('list', '--xml', '--recursive', '-r', revision, url + '@' + str(revision)).findall('list/entry')
        names = [e.findtext('name') for e in all_entries]
        empty_directories = [e.findtext('name') for e in all_entries if e.get('kind') == 'dir' and
                             not any(n.startswith(e.findtext('name') + '/') for n in names)]
        property_report = []
        for path, values in properties.items():
            require(not values.get('svn:externals', '').strip(), '检测到 svn:externals：' + path, 'externals')
            if values:
                property_report.append({'路径': path, '属性': values})
        authors, revisions, property_checks = set(), {}, set()
        pending, visited = [(url, revision, info['path'])], set()
        inside = lambda path, prefix: path == prefix or path.startswith(prefix.rstrip('/') + '/')
        while pending:
            log_url, upper, scope = pending.pop()
            if (log_url, upper) in visited:
                continue
            visited.add((log_url, upper))
            log_file = self.file('log', '--xml', '--verbose', '-r', f'1:{upper}', log_url + '@' + str(upper))
            try:
                for _, entry in ET.iterparse(log_file, events=('end',)):
                    if entry.tag != 'logentry':
                        continue
                    rev = int(entry.get('revision'))
                    author = entry.findtext('author') or '(no author)'
                    authors.add(author)
                    revisions.setdefault(str(rev), {'author': author, 'date': entry.findtext('date'), 'message': entry.findtext('msg') or '',
                                                   'paths': [dict(p.attrib, path=p.text or '') for p in entry.findall('paths/path')]})
                    for path in entry.findall('paths/path'):
                        relative = path.text or ''
                        if not inside(relative, scope):
                            continue
                        copied = path.get('copyfrom-path')
                        if path.get('kind') == 'dir' and path.get('action') != 'D' and (path.get('prop-mods') != 'false' or copied):
                            property_checks.add((relative, rev, bool(copied)))
                        # git-svn 会追溯项目外的复制父历史；作者必须在执行快照生成前收齐。
                        if copied and not inside(copied, info['path']):
                            pending.append((info['root'].rstrip('/') + quote(copied, safe='/'), int(path.get('copyfrom-rev')), copied))
                    entry.clear()
            finally:
                log_file.unlink(missing_ok=True)
        for path, rev, copied_tree in sorted(property_checks):
            properties_at_revision = self.properties(info['root'] + quote(path, safe='/'), rev, recursive=copied_tree)
            for location, values in properties_at_revision.items():
                require(not values.get('svn:externals', '').strip(), f'历史修订 r{rev} 存在 svn:externals：{location}', 'externals')
        # HTTP SVN 的属性与变更路径返回顺序不保证稳定；指纹只比较内容，
        # 不能把同一份数据的不同排列误报为来源变化。
        for history in revisions.values():
            history['paths'].sort(key=lambda row: tuple(sorted(row.items())))
        info.update(branches=sorted(branches), tags=sorted(tags), authors=sorted(authors), history=revisions,
                    properties=sorted(property_report, key=lambda row: row['路径']),
                    empty_directories=sorted(empty_directories), layout=layout)
        info['fingerprint'] = digest(info)
        return info

    def unchanged(self, snapshot):
        actual = self.info(snapshot['url'])
        head = self.info(snapshot['root'])
        require(actual['uuid'] == snapshot['uuid'] and actual['root'] == snapshot['root'] and head['revision'] == snapshot['revision'],
                '来源 UUID、地址或修订号已变化，请重新规划迁移', 'source_changed', 409)


def convert(source, snapshot, authors, folder, event):
    folder = Path(folder)
    checkout = folder / 'converted'
    if checkout.exists():
        shutil.rmtree(checkout)
    authors_file = folder / 'authors.txt'
    authors_file.write_text(''.join(f'{key} = {row["name"]} <{row["email"]}>\n' for key, row in sorted(authors.items())))
    # 从仓库根建立 RA 会话，以完整相对路径选择项目，兼容晚于 r1 创建及含中文、空格的项目。
    prefix = snapshot['path'].strip('/')
    selected = lambda path: (prefix if path == '.' else (prefix + '/' if prefix else '') + path)
    args = ['git', 'svn', 'clone', '--no-minimize-url', '--prefix=svn/', '--trunk=' + selected(snapshot['layout']['trunk'])]
    for key in ('branches', 'tags'):
        if snapshot['layout'][key]:
            args.append('--' + key + '=' + selected(snapshot['layout'][key]))
    args += ['--authors-file=' + str(authors_file), '--username=' + source.record['username'], '--no-auth-cache',
             '--config-dir=' + str(source.config), '-r', '1:' + str(snapshot['revision']), '--', snapshot['root'], str(checkout)]
    event('正在转换 SVN 历史，修订范围 1—' + str(snapshot['revision']))
    log_file = source.process.run_file(args, env=source.clone_env)
    # 原始转换输出属于诊断数据，不把命令环境或凭据写进报告。
    log_file.replace(folder / 'conversion.log')
    refs = {'refs/heads/' + snapshot['layout']['default_branch']: 'refs/remotes/svn/trunk'}
    refs.update({'refs/heads/' + n: 'refs/remotes/svn/' + n for n in snapshot['branches']})
    refs.update({'refs/tags/' + n: 'refs/remotes/svn/tags/' + n for n in snapshot['tags']})
    if snapshot.get('project_ref'):
        # 同仓库额外保留整个项目的每次历史，test_spec 等主干外文件不会丢失。
        remote = 'refs/remotes/svn-project/root'
        source.process.run(['git', 'config', 'svn-remote.project.url', snapshot['root']], cwd=checkout)
        source.process.run(['git', 'config', 'svn-remote.project.fetch', prefix + ':' + remote], cwd=checkout)
        source.process.run(['git', 'svn', 'fetch', '--authors-file=' + str(authors_file),
                            '--username=' + source.record['username'], '--no-auth-cache',
                            '--config-dir=' + str(source.config), '-r', '1:' + str(snapshot['revision']), 'project'],
                           cwd=checkout, env=source.clone_env)
        refs[snapshot['project_ref']] = remote
    imported = source.process.run(['git', 'for-each-ref', '--format=%(refname)', 'refs/remotes/svn/'], cwd=checkout).decode().splitlines()
    for original in imported:
        if original not in refs.values():
            target = 'refs/heads/svn-archive/' + original.removeprefix('refs/remotes/svn/')
            require(target not in refs, '归档历史引用与现有分支冲突', 'layout')
            refs[target] = original
    expected = {}
    for target, original in refs.items():
        source.process.run(['git', 'check-ref-format', target])
        sha = source.process.run(['git', 'rev-parse', '--verify', original + '^{commit}'], cwd=checkout).decode().strip()
        source.process.run(['git', 'update-ref', target, sha], cwd=checkout)
        expected[target] = sha
    source.process.run(['git', 'fsck', '--full'], cwd=checkout)
    strip_checkout(checkout)
    return expected


def strip_checkout(checkout):
    """仅保留转换器生成的对象库，避免工作副本符号链接进入升级卷快照。"""
    checkout = Path(checkout)
    if not checkout.is_dir():
        return
    for path in checkout.iterdir():
        if path.name == '.git':
            continue
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)


def reap_processes(root):
    """执行锁到手后只终止带本任务随机标记的孤儿，不按旧 PID 猜测进程身份。"""
    for marker in Path(root).rglob('.process-*.json'):
        record = __import__('json').loads(marker.read_text())
        tag = ('GM_SVN_PROCESS_TAG=' + record['标识']).encode()
        for process in Path('/proc').glob('[0-9]*'):
            try:
                if tag in (process / 'environ').read_bytes().split(b'\0'):
                    os.killpg(os.getpgid(int(process.name)), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                continue
        marker.unlink(missing_ok=True)
    for checkout in Path(root).glob('run-*/item-*/converted'):
        strip_checkout(checkout)
    for directory in Path(root).rglob('credentials-*'):
        if directory.is_dir():
            shutil.rmtree(directory)


def file_hash(path):
    sha = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            sha.update(chunk)
    return sha.hexdigest()


def revision_coverage(source, snapshot, mappings):
    """从 SVN 变更反向检查导入覆盖，防止只检查已有 Git 提交而漏掉整段历史。"""
    present = {(m['路径'], m['修订']) for m in mappings}
    roots = {k: (snapshot['path'].rstrip('/') if snapshot['layout'][k] == '.' else snapshot['path'].rstrip('/') + '/' + snapshot['layout'][k])
             for k in ('trunk', 'branches', 'tags') if snapshot['layout'][k]}
    result = []

    def branch(path):
        for kind, root in roots.items():
            if kind == 'trunk' and (path == root or path.startswith(root + '/')):
                return root
            if kind != 'trunk' and path.startswith(root + '/'):
                return root + '/' + path[len(root) + 1:].split('/')[0]
        return None

    def has_files(path, revision):
        url = snapshot['root'].rstrip('/') + quote(path, safe='/')
        return any(e.get('kind') == 'file' for e in source.xml(
            'list', '--xml', '--recursive', '-r', revision, url + '@' + str(revision)).findall('list/entry'))

    for number, log in sorted(snapshot['history'].items(), key=lambda row: int(row[0])):
        rev, required, explanations = int(number), set(), set()
        for change in log['paths']:
            path = change['path']; root = branch(path)
            if root is None:
                # 布局根的整体复制或替换会影响其内部，不能误认为无关目录操作。
                descendants = [r for r in roots.values() if r.startswith(path.rstrip('/') + '/')]
                if descendants and (change.get('copyfrom-path') or change.get('action') == 'R'):
                    require(any(m['修订'] == rev for m in mappings), f'布局祖先复制或替换的修订未导入：r{rev}', 'verification')
                    explanations.add('布局祖先复制或替换，已核对导入修订')
                else:
                    explanations.add('布局容器或选定布局之外的路径：' + path)
                continue
            url = source_url(snapshot['root'].rstrip('/') + quote(root, safe='/'))
            if (url, rev) in present:
                required.add(url)
                continue
            if change.get('kind') == 'file':
                # 纯 SVN 属性不一定形成 Git 提交；可执行位和符号链接仍必须核对。
                if change.get('action') == 'M' and change.get('text-mods') == 'false':
                    file_url = source_url(snapshot['root'].rstrip('/') + quote(path, safe='/'))
                    before = source.properties(file_url, rev - 1, recursive=False).get(file_url, {})
                    after = source.properties(file_url, rev, recursive=False).get(file_url, {})
                    if all((k in before) == (k in after) for k in ('svn:executable', 'svn:special')):
                        explanations.add('仅 SVN 属性变化：' + path)
                        continue
                required.add(url)
            elif change.get('action') == 'D':
                if has_files(path, rev - 1):
                    if path == root:
                        require(any(m['路径'] == url and m['修订'] < rev for m in mappings),
                                f'已删除分支缺少可达的历史提交：r{rev}，{path}', 'verification')
                        explanations.add('删除分支，删除前历史已保留：' + path)
                    else:
                        required.add(url)
                else:
                    explanations.add('删除空目录：' + path)
            elif change.get('action') in ('A', 'R') and change.get('copyfrom-path') and has_files(path, rev):
                required.add(url)
            else:
                explanations.add('仅目录或目录属性操作：' + path)
        missing = [url for url in required if (url, rev) not in present]
        require(not missing, f'SVN 文件变更没有对应的 Git 提交：r{rev}，' + '、'.join(missing), 'verification')
        result.append({'修订': rev, '已映射路径': sorted(required), '未生成独立提交的原因': sorted(explanations)})
    return result


def verify_history(source, snapshot, authors, folder, expected, event):
    checkout = Path(folder) / 'converted'
    commits = source.process.run(['git', 'rev-list', *expected], cwd=checkout).decode().splitlines()
    blob_cache, tree_cache, mappings = {}, {}, []
    for index, sha in enumerate(commits):
        raw = source.process.run(['git', 'show', '-s', '--format=%an%x00%ae%x00%at%x00%B', sha], cwd=checkout).decode('utf-8')
        name, email, timestamp, message = raw.split('\0', 3)
        match = re.search(r'\ngit-svn-id: (.+)@(\d+) ([0-9a-f-]+)\s*$', message)
        require(match and match[3] == snapshot['uuid'], '提交缺少可信 SVN 修订映射：' + sha, 'verification')
        url, rev = source_url(match[1]), int(match[2])
        require(within_source(url, snapshot['root']) and rev <= snapshot['revision'], '提交包含计划外来源或修订', 'verification')
        history = snapshot['history'].get(str(rev))
        if history is None:
            entry = source.xml('log', '--xml', '-r', rev, url + '@' + str(rev)).find('logentry')
            require(entry is not None, '复制来源历史不可读取', 'verification')
            history = {'author': entry.findtext('author') or '(no author)', 'date': entry.findtext('date'), 'message': entry.findtext('msg') or ''}
        identity = authors.get(history['author'])
        require(identity is not None, '复制来源作者未在计划中，请补充作者映射', 'verification')
        expected_time = int(datetime.fromisoformat(history['date'].replace('Z', '+00:00')).timestamp())
        require((name, email, int(timestamp)) == (identity['name'], identity['email'], expected_time), '提交作者或时间与 SVN 不一致：' + sha, 'verification')
        require(message[:match.start()].rstrip() == history['message'].rstrip(), '提交日志与 SVN 不一致：' + sha, 'verification')
        tree = source.process.run(['git', 'rev-parse', sha + '^{tree}'], cwd=checkout).decode().strip()
        key = (url, rev)
        if key not in tree_cache:
            entries = source.xml('list', '--xml', '--recursive', '-r', rev, url + '@' + str(rev)).findall('list/entry')
            props = source.properties(url, rev)
            actual = {}
            for entry in entries:
                if entry.get('kind') != 'file':
                    continue
                relative = entry.findtext('name')
                file_url = url + '/' + quote(relative, safe='/')
                values = props.get(file_url, {})
                mode = '120000' if 'svn:special' in values else '100755' if 'svn:executable' in values else '100644'
                blob_key = (file_url, entry.find('commit').get('revision'), mode)
                if blob_key not in blob_cache:
                    file = source.file('cat', '--ignore-keywords', '-r', rev, file_url + '@' + str(rev))
                    try:
                        if mode == '120000':
                            content = file.read_bytes()
                            require(content.startswith(b'link '), '符号链接存储格式无效', 'verification')
                            blob_cache[blob_key] = hashlib.sha256(content[5:]).hexdigest()
                        else:
                            blob_cache[blob_key] = file_hash(file)
                    finally:
                        file.unlink(missing_ok=True)
                actual[relative] = (mode, blob_cache[blob_key])
            tree_cache[key] = actual
        git_files = {}
        for row in source.process.run(['git', 'ls-tree', '-rz', sha], cwd=checkout).split(b'\0'):
            if not row:
                continue
            metadata, path = row.split(b'\t', 1)
            mode, kind, blob = metadata.decode().split()
            require(kind == 'blob', '遇到未支持的 Git 子模块', 'verification')
            blob_key = ('git', blob)
            if blob_key not in blob_cache:
                file = source.process.run_file(['git', 'cat-file', 'blob', blob], cwd=checkout)
                try:
                    blob_cache[blob_key] = file_hash(file)
                finally:
                    file.unlink(missing_ok=True)
            git_files[path.decode('utf-8')] = (mode, blob_cache[blob_key])
        require(git_files == tree_cache[key], f'文件内容或模式不一致：r{rev}，Git {sha}', 'verification')
        mappings.append({'提交': sha, '修订': rev, '路径': url, '树': tree})
        event(f'已逐提交验证 {index + 1}/{len(commits)}')
    coverage = revision_coverage(source, snapshot, mappings)
    project_coverage = []
    if snapshot.get('project_ref'):
        # 单独反查完整项目历史，不能仅凭主干映射充当外围目录已导入的证明。
        whole = dict(snapshot, layout={'trunk': '.', 'branches': '', 'tags': ''})
        project_coverage = revision_coverage(source, whole, mappings)
    result = {'提交数': len(commits), '已验证提交数': len(mappings), '映射': mappings, '修订覆盖': coverage,
              '完整项目修订覆盖': project_coverage, '完整项目引用': snapshot.get('project_ref'),
              '说明': '逐提交比对文件集合、SHA-256、模式、作者、时间和日志；SVN 合并图不承诺还原。'}
    atomic_json(Path(folder) / 'history-verification.json', result)
    return result
