import io
import os
import re
import shlex
import subprocess
import tarfile
import tempfile
from datetime import datetime

import requests

from models import db, GiteaServer, CommitMessageRule, CommitGateAssignment
from services.docker_service import local_exec, _get_client
from services.ssh_service import SSHService


OLD_DEFAULT_PATTERN = r'^\[ID-\d+\]\s+(feat|fix|docs|style|refactor|perf|test|chore|build|ci|revert)(\([A-Za-z0-9_.-]+\))?: .+'
OLD_DEFAULT_REJECT_MESSAGE = 'Commit message must match: [ID-xxx] type(scope): subject'
DEFAULT_PATTERN = r'^\[ID-[0-9]+\][[:space:]]+(feat|fix|docs|style|refactor|perf|test|chore|build|ci|revert)(\([A-Za-z0-9_.-]+\))?: .+'
DEFAULT_REJECT_MESSAGE = 'Commit message must match: [ID-123] type(scope): subject'
HOOK_NAME = 'gitea-manager-commit-msg'
REPO_ROOT = '/data/git/repositories'


def _ensure_url(url):
    if not url.startswith(('http://', 'https://')):
        url = 'http://' + url
    return url


def _normalize_repo_name(repo_name):
    name = (repo_name or '').replace('\\', '/').strip('/')
    if not name or name.startswith('/') or '..' in name.split('/'):
        raise ValueError('Invalid repo name')
    if any(ord(ch) < 32 for ch in name):
        raise ValueError('Invalid repo name')
    if '/' not in name:
        raise ValueError('Repo name must be owner/repo')
    return name


def _repo_path(repo_name):
    name = _normalize_repo_name(repo_name)
    return f'{REPO_ROOT}/{name}.git'


def _run_in_gitea(server, shell_script):
    shell_cmd = 'sh -c ' + shlex.quote(shell_script)
    if server.is_local:
        exit_code, out = local_exec(server.gitea_container, shell_cmd)
        return exit_code, out, ''

    ssh = SSHService(server.host, server.ssh_port, server.ssh_user)
    return ssh.exec(f'docker exec {shlex.quote(server.gitea_container)} {shell_cmd}')


def _resolve_repo_dir(server, repo_name):
    path = _repo_path(repo_name)
    lower_path = path.lower()
    script = (
        f'p={shlex.quote(path)}; '
        f'lp={shlex.quote(lower_path)}; '
        'if [ -d "$p" ]; then printf "%s" "$p"; '
        'elif [ -d "$lp" ]; then printf "%s" "$lp"; '
        'else exit 2; fi'
    )
    exit_code, out, err = _run_in_gitea(server, script)
    if exit_code != 0:
        raise FileNotFoundError(f'Repository path not found: {repo_name}')
    return out.strip()


def _put_content_local(container_name, content, dest_path):
    data = content.encode('utf-8')
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode='w') as tar:
        info = tarfile.TarInfo(name=os.path.basename(dest_path))
        info.size = len(data)
        info.mtime = int(datetime.utcnow().timestamp())
        tar.addfile(info, io.BytesIO(data))
    tar_stream.seek(0)

    client = _get_client()
    container = client.containers.get(container_name)
    container.put_archive(os.path.dirname(dest_path), tar_stream)


def _put_content_remote(server, content, dest_path):
    ssh = SSHService(server.host, server.ssh_port, server.ssh_user)
    remote_tmp = f'/tmp/{HOOK_NAME}-{server.id}-{int(datetime.utcnow().timestamp())}'
    fd, local_tmp = tempfile.mkstemp(prefix='gitea-manager-hook-', text=True)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write(content)
        ssh.put_file(local_tmp, remote_tmp)
        cmd = (
            f'docker cp {shlex.quote(remote_tmp)} '
            f'{shlex.quote(server.gitea_container)}:{shlex.quote(dest_path)}'
        )
        exit_code, out, err = ssh.exec(cmd)
        if exit_code != 0:
            raise RuntimeError(f'docker cp failed: {err or out}')
    finally:
        try:
            os.remove(local_tmp)
        except OSError:
            pass
        try:
            ssh.exec(f'rm -f {shlex.quote(remote_tmp)}')
        except Exception:
            pass


def _build_hook(rule):
    pattern = shlex.quote(rule.pattern)
    reject_message = shlex.quote(rule.reject_message or DEFAULT_REJECT_MESSAGE)
    return f"""#!/bin/sh
PATTERN={pattern}
REJECT_MESSAGE={reject_message}
ZERO=0000000000000000000000000000000000000000
STATUS=0

while read OLDREV NEWREV REFNAME; do
  if [ "$NEWREV" = "$ZERO" ]; then
    continue
  fi

  if [ "$OLDREV" = "$ZERO" ]; then
    COMMITS=$(git rev-list "$NEWREV" --not --all 2>/dev/null)
  else
    COMMITS=$(git rev-list "$OLDREV..$NEWREV" 2>/dev/null)
  fi

  for COMMIT in $COMMITS; do
    SUBJECT=$(git log -1 --format=%s "$COMMIT")
    printf '%s\\n' "$SUBJECT" | grep -Eq -- "$PATTERN"
    if [ $? -ne 0 ]; then
      echo "remote: $REJECT_MESSAGE" >&2
      echo "remote: rejected commit $COMMIT on $REFNAME" >&2
      echo "remote: subject: $SUBJECT" >&2
      STATUS=1
    fi
  done
done

exit $STATUS
"""


def _install_hook(server, repo_name, rule):
    repo_dir = _resolve_repo_dir(server, repo_name)
    hook_dir = f'{repo_dir}/hooks/pre-receive.d'
    hook_path = f'{hook_dir}/{HOOK_NAME}'
    exit_code, out, err = _run_in_gitea(server, f'mkdir -p {shlex.quote(hook_dir)}')
    if exit_code != 0:
        raise RuntimeError(err or out or 'mkdir failed')

    content = _build_hook(rule)
    if server.is_local:
        _put_content_local(server.gitea_container, content, hook_path)
    else:
        _put_content_remote(server, content, hook_path)

    chmod_cmd = (
        f'chmod +x {shlex.quote(hook_path)} && '
        f'(chown git:git {shlex.quote(hook_path)} 2>/dev/null || true)'
    )
    exit_code, out, err = _run_in_gitea(server, chmod_cmd)
    if exit_code != 0:
        raise RuntimeError(err or out or 'chmod failed')


def _remove_hook(server, repo_name):
    repo_dir = _resolve_repo_dir(server, repo_name)
    hook_path = f'{repo_dir}/hooks/pre-receive.d/{HOOK_NAME}'
    exit_code, out, err = _run_in_gitea(server, f'rm -f {shlex.quote(hook_path)}')
    if exit_code != 0:
        raise RuntimeError(err or out or 'remove hook failed')


def fetch_repos(server):
    headers = {'Authorization': f'token {server.api_token}'}
    repos = []
    page = 1
    while True:
        resp = requests.get(
            f'{_ensure_url(server.gitea_url)}/api/v1/repos/search',
            headers=headers,
            params={'page': page, 'limit': 50},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f'Gitea API returned {resp.status_code}: {resp.text[:200]}')
        items = resp.json().get('data', [])
        if not items:
            break
        repos.extend(items)
        if len(items) < 50:
            break
        page += 1
    return repos


def list_repos_with_assignments(server_id):
    server = GiteaServer.query.get(server_id)
    if not server:
        raise ValueError('Server not found')
    repos = fetch_repos(server)
    assignments = {
        a.repo_name: a
        for a in CommitGateAssignment.query.filter_by(server_id=server_id).all()
    }
    result = []
    for repo in repos:
        repo_name = repo.get('full_name', '')
        assignment = assignments.get(repo_name)
        rule = assignment.rule if assignment else None
        result.append({
            'repo_name': repo_name,
            'private': repo.get('private', False),
            'default_branch': repo.get('default_branch', ''),
            'assignment_id': assignment.id if assignment else None,
            'rule_id': assignment.rule_id if assignment else None,
            'rule_name': rule.name if rule else '',
            'install_status': assignment.install_status if assignment else '',
            'install_log': assignment.install_log if assignment else '',
            'applied_at': assignment.applied_at.isoformat() if assignment and assignment.applied_at else None,
        })
    return result


def apply_rule(server_id, rule_id, repo_names=None, apply_all=False):
    server = GiteaServer.query.get(server_id)
    rule = CommitMessageRule.query.get(rule_id)
    if not server or not rule or rule.server_id != server_id:
        raise ValueError('Server or rule not found')
    if not rule.enabled:
        raise ValueError('Rule is disabled')

    if apply_all:
        repo_names = [r.get('full_name', '') for r in fetch_repos(server)]
    repo_names = [_normalize_repo_name(r) for r in (repo_names or []) if r]
    if not repo_names:
        raise ValueError('No repositories selected')

    results = []
    for repo_name in repo_names:
        assignment = CommitGateAssignment.query.filter_by(
            server_id=server_id,
            repo_name=repo_name,
        ).first()
        if not assignment:
            assignment = CommitGateAssignment(
                server_id=server_id,
                repo_name=repo_name,
                rule_id=rule.id,
                created_at=datetime.utcnow(),
            )
            db.session.add(assignment)
        assignment.rule_id = rule.id
        assignment.install_status = 'pending'
        assignment.install_log = ''
        assignment.applied_at = datetime.utcnow()
        db.session.commit()

        try:
            _install_hook(server, repo_name, rule)
            assignment.install_status = 'success'
            assignment.install_log = 'Hook installed'
        except Exception as e:
            assignment.install_status = 'failed'
            assignment.install_log = str(e)[:500]
        assignment.applied_at = datetime.utcnow()
        db.session.commit()
        results.append(assignment_to_dict(assignment))
    return results


def remove_gate(server_id, repo_names=None, remove_all=False):
    server = GiteaServer.query.get(server_id)
    if not server:
        raise ValueError('Server not found')
    if remove_all:
        assignments = CommitGateAssignment.query.filter_by(server_id=server_id).all()
        repo_names = [a.repo_name for a in assignments]
    repo_names = [_normalize_repo_name(r) for r in (repo_names or []) if r]
    if not repo_names:
        raise ValueError('No repositories selected')

    results = []
    for repo_name in repo_names:
        assignment = CommitGateAssignment.query.filter_by(
            server_id=server_id,
            repo_name=repo_name,
        ).first()
        try:
            _remove_hook(server, repo_name)
            if assignment:
                db.session.delete(assignment)
                db.session.commit()
            results.append({'repo_name': repo_name, 'status': 'success', 'log': 'Hook removed'})
        except FileNotFoundError as e:
            if assignment:
                db.session.delete(assignment)
                db.session.commit()
            results.append({'repo_name': repo_name, 'status': 'success', 'log': str(e)})
        except Exception as e:
            if assignment:
                assignment.install_status = 'failed'
                assignment.install_log = str(e)[:500]
                db.session.commit()
            results.append({'repo_name': repo_name, 'status': 'failed', 'log': str(e)[:500]})
    return results


def test_message(pattern, message):
    try:
        completed = subprocess.run(
            ['grep', '-Eq', '--', pattern or ''],
            input=(message or '') + '\n',
            text=True,
            capture_output=True,
            timeout=5,
        )
        if completed.returncode in (0, 1):
            return {'ok': True, 'matched': completed.returncode == 0}
        return {'ok': False, 'error': (completed.stderr or 'Invalid grep -E pattern').strip()}
    except FileNotFoundError:
        pass
    except Exception as e:
        return {'ok': False, 'error': str(e)}

    try:
        fallback_pattern = _python_regex_from_grep_ere(pattern or '')
        matched = re.search(fallback_pattern, message or '') is not None
        return {'ok': True, 'matched': matched}
    except re.error as e:
        return {'ok': False, 'error': str(e)}


def _python_regex_from_grep_ere(pattern):
    replacements = {
        '[[:space:]]': r'\s',
        '[[:digit:]]': r'\d',
        '[[:alnum:]]': r'[A-Za-z0-9]',
        '[[:alpha:]]': r'[A-Za-z]',
        '[[:lower:]]': r'[a-z]',
        '[[:upper:]]': r'[A-Z]',
        '[[:xdigit:]]': r'[A-Fa-f0-9]',
    }
    for source, target in replacements.items():
        pattern = pattern.replace(source, target)
    return pattern


def rule_to_dict(rule):
    assignment_count = CommitGateAssignment.query.filter_by(rule_id=rule.id).count()
    return {
        'id': rule.id,
        'server_id': rule.server_id,
        'server_name': rule.server.name if rule.server else '',
        'name': rule.name,
        'pattern': rule.pattern,
        'reject_message': rule.reject_message,
        'enabled': rule.enabled,
        'assignment_count': assignment_count,
        'created_at': rule.created_at.isoformat() if rule.created_at else None,
        'updated_at': rule.updated_at.isoformat() if rule.updated_at else None,
    }


def assignment_to_dict(assignment):
    return {
        'id': assignment.id,
        'server_id': assignment.server_id,
        'repo_name': assignment.repo_name,
        'rule_id': assignment.rule_id,
        'rule_name': assignment.rule.name if assignment.rule else '',
        'install_status': assignment.install_status,
        'install_log': assignment.install_log,
        'applied_at': assignment.applied_at.isoformat() if assignment.applied_at else None,
        'created_at': assignment.created_at.isoformat() if assignment.created_at else None,
    }
