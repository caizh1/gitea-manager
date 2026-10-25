"""在相同真实数据上切换密钥来源，验证解密、重建及恢复持久性。"""
import json
import hashlib
import time

from .driver import CheckFailure
from .oracle import verify_identity


def matrix(lab):
    from .checkpoint import empty_target
    lab.login()
    with lab.ssh('a') as client:
        with client.open_sftp() as sftp:
            with sftp.file('/lab/compose.json', 'rb') as stream:
                original = stream.read()
    lab.command('a', ['mkdir', '-p', '/lab/secrets'])
    lab.put('a', '/lab/secrets/source-key', lab.state['nodes']['a']['secret'])
    lab.command('a', ['chown', '1000:1000', '/lab/secrets/source-key'])
    lab.command('a', ['chmod', '400', '/lab/secrets/source-key'])
    try:
        for mode in ('ini', 'environment', 'uri', 'file'):
            with lab.case('secrets-partial', '密钥来源：' + mode):
                config = lab.compose_config('a', original)
                service = config['services']['gitea']
                environment = service['environment']
                for key in ('GITEA__security__SECRET_KEY', 'GITEA__security__SECRET_KEY_URI', 'GITEA__security__SECRET_KEY__FILE'):
                    environment.pop(key, None)
                if mode == 'environment':
                    environment['GITEA__security__SECRET_KEY'] = lab.state['nodes']['a']['secret']
                elif mode in ('uri', 'file'):
                    service['volumes'].append('/lab/secrets/source-key:/external-secret:ro')
                    key = 'GITEA__security__SECRET_KEY_URI' if mode == 'uri' else 'GITEA__security__SECRET_KEY__FILE'
                    environment[key] = 'file:/external-secret' if mode == 'uri' else '/external-secret'
                    if mode == 'uri':
                        environment['GITEA__security__SECRET_KEY'] = ''
                # 显式清除上一轮遗留 URI；普通配置中的密钥值保持不变。
                lab.command('a', ['docker', 'exec', 'gitea', 'sed', '-i', '/^SECRET_KEY_URI[[:space:]]*=/d', '/data/gitea/conf/app.ini'])
                if mode == 'ini':
                    lab.command('a', ['docker', 'exec', '-e', 'GITEA__security__SECRET_KEY=' + lab.state['nodes']['a']['secret'],
                                     '-e', 'GITEA__security__SECRET_KEY_URI=',
                                     'gitea', 'environment-to-ini', '--config', '/data/gitea/conf/app.ini'])
                lab.put('a', '/lab/compose.json', json.dumps(config))
                lab.command('a', ['docker', 'compose', '-f', '/lab/compose.json', 'up', '-d', '--force-recreate', 'gitea'])
                try:
                    lab.wait_gitea('a')
                except Exception:
                    # 重建前保留首次启动错误，不能在 finally 重建后只剩健康日志。
                    logs = lab.command('a', ['docker', 'logs', '--tail', '120', 'gitea'], check=False)
                    path = lab.root / ('secret-source-startup-' + mode + '-' + str(time.time_ns()) + '.json')
                    path.write_text(json.dumps({'说明': '切换源端密钥来源后的启动错误，尚未请求 Manager 备份',
                        '密钥来源': mode, '日志': lab.scrub((logs[1] + logs[2]).decode(errors='replace'))}, ensure_ascii=False, indent=2) + '\n')
                    path.chmod(0o600)
                    raise
                verify_identity(lab, 'a')
                lab.backup('a')
                with empty_target(lab):
                    if lab.state['nodes']['a']['secret'] == lab.state['nodes']['b']['secret']:
                        raise CheckFailure('源目标密钥意外相同，不能验证密钥切换')
                    expected = hashlib.sha256(lab.state['nodes']['a']['secret'].encode()).hexdigest()
                    task = lab.restore('b')
                    if task['status'] != 'success':
                        raise CheckFailure('密钥来源恢复失败：' + task.get('error_msg', ''))
                    verify_identity(lab, 'b')
                    lab.command('b', ['docker', 'compose', '-f', '/lab/compose.json', 'up', '-d', '--force-recreate', 'gitea'])
                    lab.wait_gitea('b')
                    verify_identity(lab, 'b')
                    from .oracle import snapshot
                    if snapshot(lab, 'b')['数据密钥']['security.secret_key'] != expected:
                        raise CheckFailure('Compose 重建后实际数据密钥不再是源密钥')
    finally:
        lab.command('a', ['docker', 'exec', 'gitea', 'sed', '-i', '/^SECRET_KEY_URI[[:space:]]*=/d', '/data/gitea/conf/app.ini'], check=False)
        lab.put('a', '/lab/compose.json', original)
        lab.command('a', ['docker', 'compose', '-f', '/lab/compose.json', 'up', '-d', '--force-recreate', 'gitea'])
        lab.wait_gitea('a')
    lab.note('secrets', '四种密钥来源及重建后解密', '通过', '真实双因素认证与只读令牌边界均通过')
