"""对真实隔离实例做可逆破坏，检查独立判定器是否确实检出。"""
import contextlib
import configparser
import json
import time
from urllib.parse import urlparse, unquote

from . import oracle
from .driver import CheckFailure


@contextlib.contextmanager
def hidden_file(lab, node, path):
    hidden = path + '.oracle-hidden'
    lab.command(node, ['docker', 'exec', 'gitea', 'mv', path, hidden])
    try:
        yield
    finally:
        lab.command(node, ['docker', 'exec', 'gitea', 'mv', hidden, path])


def run(lab):
    node = 'b'
    baseline = oracle.snapshot(lab, node)
    outputs = []

    def check(label, section, mutation):
        with lab.case('oracle-partial', label):
            with mutation:
                differences = oracle.compare(baseline, oracle.snapshot(lab, node))
                if not any(item['范围'] == section for item in differences):
                    raise CheckFailure('独立核验器没有检出真实破坏：' + label)
                outputs.append({'变异': label, '检出差异': differences})

    @contextlib.contextmanager
    def tag():
        path, repo = next((path, repo) for path, repo in baseline['Git仓库'].items()
                          if any(ref.startswith('refs/tags/') for ref in repo['refs']))
        ref, sha = next(ref.split() for ref in repo['refs'] if ref.startswith('refs/tags/'))
        command = ['docker', 'exec', '-u', 'git', 'gitea', 'git', '--git-dir', '/data/git/repositories/' + path, 'update-ref']
        lab.command(node, command + ['-d', ref])
        try:
            yield
        finally:
            lab.command(node, command + [ref, sha])

    check('真实删除标签', 'Git仓库', tag())
    for label, prefix in [('真实隐藏软件包正文', '/data/gitea/packages/'),
                          ('真实隐藏 LFS 对象', '/data/git/lfs/'),
                          ('真实隐藏流水线日志', '/data/gitea/actions_log/')]:
        paths = sorted(path for path in baseline['正文文件'] if path.startswith(prefix))
        if not paths:
            raise CheckFailure('验证器反证缺少真实样本：' + label)
        check(label, '正文文件', hidden_file(lab, node, paths[0]))

    @contextlib.contextmanager
    def permission():
        users = {row['name']: row['id'] for row in baseline['业务表']['user']}
        repository = next(row for row in baseline['业务表']['repository']
                          if row['name'] == 'proof' and row['owner_id'] == users['labadmin'])
        saved = next(row for row in baseline['业务表']['collaboration']
                     if row['repo_id'] == repository['id'] and row['user_id'] == users['labreader'])
        # 模拟备份中权限记录被篡改，直接改一个字段，避免 API 合法追加审计干扰清理对照。
        oracle.sql(lab, node, 'UPDATE collaboration SET mode=4 WHERE id=%d' % saved['id'])
        try:
            yield
        finally:
            oracle.sql(lab, node, 'UPDATE collaboration SET mode=%d WHERE id=%d' % (saved['mode'], saved['id']))

    check('真实扩大只读成员权限', '业务表', permission())

    @contextlib.contextmanager
    def setting(key, value):
        path = '/data/gitea/conf/app.ini'
        original = lab.command(node, ['docker', 'exec', 'gitea', 'cat', path])[1]
        script = '/^' + key + '[[:space:]]*=/s/=.*/= ' + value + '/'
        lab.command(node, ['docker', 'exec', 'gitea', 'sed', '-i', script, path])
        try:
            yield
        finally:
            lab.command(node, ['docker', 'exec', '-i', 'gitea', 'sh', '-c', 'cat > "$1"', 'sh', path], input_data=original.decode())

    check('真实修改功能开关', '业务配置', setting('DISABLE_REGISTRATION', 'oracle-invalid'))

    @contextlib.contextmanager
    def secret():
        raw = lab.command(node, ['docker', 'exec', 'gitea', 'cat', '/data/gitea/conf/app.ini'])[1].decode()
        config = configparser.ConfigParser(interpolation=None)
        config.read_string('[DEFAULT]\n' + raw)
        uri = config.get('security', 'SECRET_KEY_URI', fallback='')
        if not uri:
            with setting('SECRET_KEY', 'oracle-deliberately-wrong'):
                yield
            return
        path = unquote(urlparse(uri).path)
        original = lab.command(node, ['docker', 'exec', 'gitea', 'cat', path])[1].decode()
        args = ['docker', 'exec', '-i', 'gitea', 'sh', '-c', 'cat > "$1"', 'sh', path]
        lab.command(node, args, input_data='oracle-deliberately-wrong')
        try:
            yield
        finally:
            lab.command(node, args, input_data=original)

    check('真实修改加密密钥', '数据密钥', secret())
    final = oracle.compare(baseline, oracle.snapshot(lab, node))
    if final:
        raise CheckFailure('反证清理后仍有非预期差异：' + json.dumps(final, ensure_ascii=False))
    (lab.root / ('oracle-live-challenges-' + str(time.time_ns()) + '.json')).write_text(json.dumps({'说明': '七类真实可逆破坏均被独立检查检出', '结果': outputs}, ensure_ascii=False, indent=2) + '\n')
    lab.note('oracle', '七类真实变异全部检出并恢复', '通过')
