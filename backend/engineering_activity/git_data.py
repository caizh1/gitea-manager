"""从固定 Git 对象生成可复算的逐文件行数。"""
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from services.statistics_service import _classify_file


OID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
FIRMWARE = {'.s', '.asm', '.inc', '.ld', '.lds', '.svh', '.vh', '.vhd', '.vhdl', '.mk', '.dts', '.dtsi', '.def'}
BUILD_FILES = {'makefile', 'gnumakefile', 'kconfig', 'cmakelists.txt', 'dockerfile', 'meson.build'}
GROUPS = ('code', 'doc', 'generated', 'third_party', 'other', 'binary')
DEFAULT_PATHS = {
    'generated': ('generated', 'dist', 'build'),
    'third_party': ('vendor', 'third_party', 'third-party', 'node_modules'),
}


class GitUnavailable(RuntimeError):
    pass


def classify(path, custom):
    lowered = path.casefold()
    parts = lowered.split('/')
    for group in ('generated', 'third_party'):
        if any(lowered == root or lowered.startswith(root + '/') for root in custom.get(group, ())):
            return group
        if any(part in DEFAULT_PATHS[group] for part in parts[:-1]):
            return group
    basename = parts[-1]
    if basename in BUILD_FILES or Path(basename).suffix in FIRMWARE:
        return 'code'
    return _classify_file(path)


def line_counts(files, settings):
    result = {group: [0, 0] for group in GROUPS}
    for path, added, deleted in files:
        if added is None:
            result['binary'][0] += 1
        else:
            bucket = result[classify(path, settings)]
            bucket[0] += added
            bucket[1] += deleted
    return result


HTTP_ONLY = ('-c', 'http.followRedirects=false', '-c', 'protocol.allow=never',
             '-c', 'protocol.http.allow=always', '-c', 'protocol.https.allow=always')


class GitData:
    def __init__(self, directory, environment=None, sha_length=40):
        self.directory = directory
        self.environment = environment
        self.sha_length = sha_length

    def run(self, *args, input_data=None, timeout=60):
        try:
            return subprocess.run(['git', '-C', str(self.directory), *args], env=self.environment,
                                  input=input_data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  timeout=timeout, check=True).stdout
        except (subprocess.SubprocessError, OSError) as exc:
            raise GitUnavailable('Git 对象缺失或读取超时') from exc

    def refs(self):
        rows = self.run('for-each-ref', '--format=%(objectname) %(refname)', 'refs/heads').decode().splitlines()
        return {name: oid for oid, name in (row.split(' ', 1) for row in rows)}

    def present(self, oids):
        if not oids:
            return set()
        results = self.run('cat-file', '--batch-check=%(objectname) %(objecttype)',
                           input_data=('\n'.join(oids) + '\n').encode())
        return {oid for oid, kind in (row.split() for row in results.decode().splitlines()) if kind == 'commit'}

    def reachable_boundary(self, previous, lookup):
        remaining = set(previous)
        checked = set()
        boundary = set()
        while remaining:
            available = self.present(sorted(remaining))
            boundary.update(available)
            missing = remaining - available
            checked.update(remaining)
            if len(checked) > 2000:
                raise GitUnavailable('旧分支对象超出可核验范围')
            remaining = {parent for oid in missing for parent in lookup(oid)} - checked
        return sorted(boundary)

    def new_commits(self, head, previous, lookup):
        previous = sorted(set(previous))
        if not OID.fullmatch(head or '') or any(not OID.fullmatch(oid or '') for oid in previous):
            raise GitUnavailable('无法确认推送对象')
        if head in previous:
            return []
        boundary = self.reachable_boundary(previous, lookup)
        revisions = '\n'.join([head, *('^' + oid for oid in boundary)]) + '\n'
        return self.run('rev-list', '--reverse', '--topo-order', '--stdin', input_data=revisions.encode()).decode().splitlines()

    def commit(self, oid):
        if not OID.fullmatch(oid or ''):
            raise GitUnavailable('无法确认提交对象')
        parents, name, subject = self.run('show', '-s', '--no-notes', '--format=%P%x00%an%x00%s', oid, '--').decode(errors='replace').strip('\n').split('\0', 2)
        parent_list = parents.split()
        if len(parent_list) > 1:
            return name, subject, True, []
        rows = self.run('diff-tree', '--root', '--no-commit-id', '--find-renames=50%', '--no-ext-diff',
                        '--no-textconv', '-r', '--numstat', '-z', oid, '--')
        files = []
        entries = iter(rows.split(b'\0'))
        for row in entries:
            if not row:
                continue
            added, deleted, path = row.split(b'\t', 2)
            if not path:
                next(entries)
                path = next(entries)
            files.append([path.decode(errors='replace'), int(added) if added != b'-' else None,
                          int(deleted) if deleted != b'-' else None])
        return name, subject, False, files


@contextmanager
def fetch(client, repository, unchanged=None):
    with tempfile.TemporaryDirectory(prefix='gm-engineering-git-') as directory:
        askpass = Path(directory, 'askpass')
        askpass.write_text('#!/bin/sh\ncase "$1" in *Username*) printf "%s\\n" oauth2 ;; *) printf "%s\\n" "$GM_ENGINEERING_TOKEN" ;; esac\n')
        askpass.chmod(0o700)
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_ASKPASS=str(askpass), GM_ENGINEERING_TOKEN=client.token, LC_ALL='C')
        git = GitData(directory, env, 64 if repository.get('object_format_name') == 'sha256' else 40)
        git.run('init', '--bare', f'--object-format=sha{256 if git.sha_length == 64 else 1}')
        path = '/'.join(quote(item, safe='') for item in repository['full_name'].split('/'))
        remote = f'{client.url}/{path}.git'
        if unchanged is not None:
            rows = git.run(*HTTP_ONLY, 'ls-remote', '--heads', '--', remote).decode().splitlines()
            remote_heads = {name: oid for oid, name in (row.split('\t', 1) for row in rows)}
            if remote_heads == unchanged:
                yield git, remote_heads
                return
        git.run(*HTTP_ONLY, 'fetch', '--no-tags', '--no-auto-gc', '--no-write-fetch-head',
                '--', remote, '+refs/heads/*:refs/heads/*', timeout=300)
        yield git, git.refs()
