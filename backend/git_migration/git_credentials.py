"""只向固定仓库提供进程内 Git 凭据，不落盘，也不叠加 LFS 返回的认证头。"""
import os
from pathlib import Path
import shlex
import sys
from urllib.parse import unquote, urlsplit


def environment(remote, username, secret):
    command = '!' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(Path(__file__).resolve()))
    return [('credential.helper', ''), ('credential.helper', command), ('credential.useHttpPath', 'true')], {
        'GM_GIT_ALLOWED_URL': remote, 'GM_GIT_USERNAME': username, 'GM_GIT_PASSWORD': secret}


def credential(request, values):
    try:
        expected = urlsplit(values.get('GM_GIT_ALLOWED_URL', ''))
        actual = urlsplit(request.get('protocol', '') + '://' + request.get('host', '') + '/' + request.get('path', '').lstrip('/'))
        def origin(url): return url.scheme.lower(), (url.hostname or '').lower().rstrip('.'), url.port or (443 if url.scheme == 'https' else 80)
        path, prefix = unquote(actual.path), unquote(expected.path).rstrip('/')
        allowed = expected.scheme in ('http','https') and origin(expected) == origin(actual) and not actual.username and not actual.password
        allowed = allowed and not actual.query and not actual.fragment and '\\' not in path and '\x00' not in path
        allowed = allowed and not any(part in ('.','..') for part in path.split('/')) and (path == prefix or path.startswith(prefix + '/'))
        username, secret = values.get('GM_GIT_USERNAME',''), values.get('GM_GIT_PASSWORD','')
        if not allowed or not username or not secret or len(username)>1024 or len(secret)>8192 or any(c in username+secret for c in '\r\n\x00'):
            return {}
        return {'username': username, 'password': secret}
    except (ValueError, UnicodeError):
        return {}


if __name__ == '__main__' and sys.argv[1:] == ['get']:
    request = dict(line.split('=',1) for line in sys.stdin.read(16384).splitlines() if '=' in line)
    result = credential(request, os.environ)
    if result:
        sys.stdout.write('username='+result['username']+'\npassword='+result['password']+'\n\n')
