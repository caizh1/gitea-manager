"""分阶段执行实际灾备；错误记录不会被后续成功覆盖。"""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import threading
import time
import zipfile

from .driver import CheckFailure
from .errors import EnvironmentBlocked
from . import fixtures, oracle


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def seed(lab):
    from . import actions, governance, manager_canaries, identity_keys, collaboration
    lab.login()
    manager_canaries.seed(lab)
    for label, function in [('用户与权限样本', fixtures.seed_users), ('双因素身份样本', fixtures.seed_twofactor), ('完整历史仓库', fixtures.seed_git),
                             ('协作样本', fixtures.seed_collaboration), ('扩展协作样本', collaboration.seed), ('LFS 样本', fixtures.seed_lfs),
                             ('真实软件包', fixtures.seed_packages), ('OCI 容器软件包', fixtures.seed_oci),
                             ('群组与审批样本', governance.seed), ('真实流水线', actions.seed), ('独立SSH身份和部署密钥', identity_keys.seed)]:
        with lab.case('seed', label):
            function(lab, 'a')
    fixtures.ensure_repo(lab, 'b', 'target-only', auto_init=True)
    fixtures.ensure_repo(lab, 'a', 'public-signin-proof', auto_init=True)
    lab.api('a', 'PATCH', '/repos/labadmin/public-signin-proof', json={'private': False})
    # 设置不同的源业务配置；恢复器若保留整个目标配置，独立对照必须报出差异。
    config = lab.compose_config('a')
    config['services']['gitea']['environment']['GITEA__service__DISABLE_REGISTRATION'] = 'false'
    config['services']['gitea']['environment']['GITEA__service__REQUIRE_SIGNIN_VIEW'] = 'true'
    lab.put('a', '/lab/compose.json', json.dumps(config))
    lab.command('a', ['docker', 'compose', '-f', '/lab/compose.json', 'up', '-d', 'gitea'])
    lab.wait_gitea('a')
    lab.api('a', 'PATCH', '/admin/users/labdisabled', json={'login_name': 'labdisabled', 'active': False})
    response = lab.request('a', 'GET', '/labadmin/public-signin-proof', headers={'Authorization': ''}, expected=None)
    if '/user/login' not in response.url:
        raise CheckFailure('源匿名访问限制没有实际生效，不能构建安全配置迁移样本')
    lab.state['settings_seeded'] = True
    lab.state['seed_complete'] = True
    lab.save()


def baseline(lab):
    if not lab.state.get('seed_complete'):
        raise CheckFailure('真实数据准备未全部完成，拒绝生成正式基线')
    with lab.case('baseline', '独立采集源与目标基线'):
        stamp = str(time.time_ns())
        current = {}
        for node in ('a', 'b'):
            oracle.settle(lab, node)
            data = oracle.snapshot(lab, node)
            path = lab.root / 'baselines' / (stamp + '-' + node + '.json')
            path.parent.mkdir(exist_ok=True)
            write_json(path, data)
            current[node] = str(path)
            initial = lab.root / ('baseline-' + node + '.json')
            if not initial.exists():
                write_json(initial, data)
        lab.state['baselines'] = current
        lab.state.pop('baseline_dirty', None)
        lab.save()
        challenge = oracle.challenge(json.loads(Path(current['a']).read_text()))
        write_json(lab.root / ('oracle-challenges-' + stamp + '.json'), {'说明': '初步四类检出，完整七类反证仍单独验收', '结果': challenge})


def download_backup(lab, identifier):
    directory = lab.root / 'backups'
    directory.mkdir(exist_ok=True)
    path = directory / ('download-%s-%s.zip' % (identifier, time.time_ns()))
    begin = time.monotonic()
    response = lab.manager_api('GET', '/backups/%s/download' % identifier, stream=True)
    digest, length = hashlib.sha256(), 0
    with path.open('wb') as stream:
        for block in response.iter_content(1024 * 1024):
            stream.write(block); digest.update(block); length += len(block)
    response.close()
    transfer_seconds = time.monotonic() - begin
    path.chmod(0o600)
    if response.headers.get('Content-Length') and length != int(response.headers['Content-Length']):
        raise CheckFailure('下载备份实际长度与响应头不一致')
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise CheckFailure('下载备份 ZIP CRC 不通过')
        manifest = json.loads(archive.read('manager-manifest.json'))
        for name, expected in manifest['文件'].items():
            with archive.open(name) as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != expected['摘要']:
                raise CheckFailure('下载备份正文摘要不匹配：' + name)
    write_json(path.with_suffix('.json'), {'说明': '实际 HTTP 下载与逐项正文校验',
               '字节': length, 'SHA256': digest.hexdigest(), '归档条目': len(manifest['文件']),
               '实际下载耗时秒': round(transfer_seconds, 3), 'HTTP平均字节每秒': round(length / transfer_seconds, 3)})
    lab.state['downloaded_backup'] = str(path)
    lab.save()
    return path


def compare_target(lab, source_baseline, node, label):
    actual = oracle.snapshot(lab, node)
    differences = oracle.compare(source_baseline, actual)
    write_json(lab.root / ('comparison-' + label + '-' + str(time.time_ns()) + '.json'), {
        '说明': '独立源基线与恢复结果对照', '源快照': source_baseline.get('证据文件'),
        '目标快照': actual.get('证据文件'), '差异': differences})
    for section, category in [('Git仓库', 'git-data'), ('业务配置', 'settings'), ('正文文件', 'content')]:
        relevant = [item for item in differences if item['范围'] == section]
        lab.note(category, label + '：' + section, '失败' if relevant else '通过',
                 json.dumps(relevant, ensure_ascii=False) if relevant else '独立基线一致')
    if any(item['范围'] == '业务表' for item in differences):
        lab.note('data', label + '：业务数据表', '失败', json.dumps([item for item in differences if item['范围'] == '业务表'], ensure_ascii=False))
    else:
        lab.note('data', label + '：业务数据表', '通过', '完整 public 数据表一致，精确排除字段已登记')
    return differences


def verify_business(lab, node, label, preparation=False):
    from . import actions, governance, identity_keys, collaboration
    results = []
    names = {'git-protocol': 'Git 协议与写入权限', 'packages-partial': '软件包正文与客户端',
             'lfs-partial': 'LFS 上传下载与子模块', 'identities-partial': '真实认证与令牌边界',
             'governance-partial': '群组继承、审批与 Hook', 'actions-partial': '流水线、日志与制品',
             'identities-ssh-partial': '独立SSH用户、部署密钥和禁用身份',
             'collaboration-extended-partial': 'Issue和PR附件、Wiki历史、关注、收藏及改名仓库'}
    for category, function in [('git-protocol', oracle.verify_git), ('packages-partial', oracle.verify_packages),
                               ('lfs-partial', oracle.verify_lfs), ('identities-partial', oracle.verify_identity),
                               ('governance-partial', governance.verify), ('actions-partial', actions.verify),
                               ('identities-ssh-partial', identity_keys.verify),
                               ('collaboration-extended-partial', collaboration.verify)]:
        try:
            with lab.case('seed' if preparation else category, label + '：' + names[category]):
                function(lab, node)
            results.append(True)
        except Exception:
            results.append(False)
    seed = lab.state['nodes'][node]['seed']
    if lab.state.get('settings_seeded'):
        try:
            with lab.case('seed' if preparation else 'settings', label + '：匿名访问边界'):
                response = lab.request(node, 'GET', '/labadmin/public-signin-proof', headers={'Authorization': ''}, expected=None)
                if '/user/login' not in response.url:
                    raise CheckFailure('源要求登录查看的仓库在恢复后允许匿名读取')
        except Exception:
            results.append(False)
    with lab.case('seed' if preparation else 'collaboration-partial', label + '：Issue 与发布附件'):
        issue = lab.api(node, 'GET', '/repos/labadmin/proof/issues/%s' % seed['issue']).json()
        if issue['body'] != '正文需要完整保留':
            raise CheckFailure('Issue 正文不一致')
        response = lab.request(node, 'GET', '/attachments/' + lab.api(node, 'GET', '/repos/labadmin/proof/releases/%s/assets/%s' % (seed['release'], seed['release_asset'])).json()['uuid'], expected=None)
        if response.status_code == 200 and hashlib.sha256(response.content).hexdigest() != seed['release_sha256']:
            raise CheckFailure('发布附件摘要不一致')
        if response.status_code != 200:
            raise CheckFailure('发布附件无法下载：%s' % response.status_code)
    if lab.state.get('manager_canaries'):
        from .manager_canaries import verify
        with lab.case('seed' if preparation else 'manager-partial', label + '：管理数据与会话'):
            verify(lab)
    if lab.state['nodes'][node].get('integrations_seeded'):
        from .integrations import verify
        try:
            with lab.case('seed' if preparation else 'integrations-partial', label + '：外部集成实际调用'):
                verify(lab, node, preparation=preparation)
        except Exception:
            results.append(False)
    return all(results)


def normal(lab):
    lab.login()
    if lab.state.get('baseline_dirty'):
        raise CheckFailure('源业务夹具已有新增写入；先重新执行 baseline，旧基线保留，尚未请求业务任务')
    expected = json.loads(Path(lab.state.get('baselines', {}).get('a', lab.root / 'baseline-a.json')).read_text())
    if expected.get('规则') != oracle.collection_rules():
        raise CheckFailure('普通迁移基线采集规则已变化；请先重新执行 baseline，旧证据保留，尚未请求备份或恢复')
    with lab.case('normal-preflight', '三轮正向、反向及独立下载的完整阶段空间'):
        # 四份 Manager 归档、三份独立下载均保留，另留一份校验副本。
        lab.ensure_space(future_backups=8)
        from .manager_canaries import verify
        verify(lab)
    lab.state['baseline_dirty'] = '普通迁移及真实业务写入已开始；下一次静态迁移前必须完整复验并重采'
    lab.save()
    for index in range(1, 4):
        label = 'A-to-B-%d' % index
        with lab.case('transfer', label + '：备份下载与恢复终态'):
            backup = lab.backup('a')
            download_backup(lab, backup['id'])
            restored = lab.restore('b')
            if restored['status'] != 'success':
                raise CheckFailure('恢复失败：' + restored.get('error_msg', ''))
            lab.wait_gitea('b')
        differences = compare_target(lab, expected, 'b', label)
        try:
            business_ok = verify_business(lab, 'b', label)
        except Exception:
            business_ok = False
        lab.note('normal', label, '通过' if not differences and business_ok else '失败',
                 '完整迁移对照通过' if not differences and business_ok else '详见差异与实际使用检查；任务成功不等于完整业务迁移通过')
    fixtures.ensure_repo(lab, 'b', 'after-restore', auto_init=True)
    expected_b = oracle.snapshot(lab, 'b')
    write_json(lab.root / ('baseline-b-new-' + str(time.time_ns()) + '.json'), expected_b)
    with lab.case('transfer', 'B-to-A：新增业务后反向恢复'):
        lab.backup('b')
        restored = lab.restore('a')
        if restored['status'] != 'success':
            raise CheckFailure(restored.get('error_msg', '反向恢复失败'))
    compare_target(lab, expected_b, 'a', 'B-to-A')
    for node in ('a', 'b'):
        with lab.case('persistence', node + '：Compose 重建后原数据可用'):
            lab.command(node, ['docker', 'compose', '-f', '/lab/compose.json', 'up', '-d', '--force-recreate', 'gitea'])
            lab.wait_gitea(node)
            oracle.verify_git(lab, node)
            oracle.verify_identity(lab, node)
            from .actions import verify as verify_actions
            verify_actions(lab, node)


def malformed_archives(lab):
    from .archive_faults import synthetic_archive
    lab.login()
    with lab.case('archive-partial', '合成归档正向对照：清单与停写快照可独立导入'):
        imported = lab.manager_api('POST', '/backups/import', files={'file': ('control.zip', synthetic_archive())}).json()
        write_json(lab.root / ('archive-control-' + str(time.time_ns()) + '.json'), {
            '说明': '仅作为导入格式正向对照，未恢复到任何实例，不代表真实业务备份',
            '导入ID': imported['id'], '占位来源ID': imported['source_server_id']})
        lab.manager_api('DELETE', '/backups/%s' % imported['id'])
        lab.manager_api('DELETE', '/servers/%s' % imported['source_server_id'])
    cases = [('截断 ZIP', b'PK\x03\x04truncated')]
    for filename, mode in [('../escape', 0o100600), ('/absolute-escape', 0o100600), ('linked', 0o120777)]:
        cases.append((filename, synthetic_archive([(filename, b'../../escape', mode)])))
    cases.append(('重复条目', synthetic_archive([('duplicate', b'first', 0o100600), ('duplicate', b'second', 0o100600)])))
    records = []
    for name, body in cases:
        with lab.case('archive-partial', '归档导入拒绝：' + name):
            response = lab.manager_api('POST', '/backups/import', files={'file': ('malformed.zip', body)}, expected=None)
            records.append({'说明': name, 'HTTP状态': response.status_code, '响应': lab.scrub(response.text[:1000])})
            write_json(lab.root / ('archive-rejections-' + str(time.time_ns()) + '.json'), {
                '说明': '正向格式对照已导入；攻击归档保留完整清单和快照，仅增加危险条目，避免因缺清单误判通过', '结果': records})
            if response.status_code not in (400, 409, 422):
                raise CheckFailure('危险归档未被安全拒绝：%d' % response.status_code)
    for node in ('a', 'b'):
        lab.wait_gitea(node)


def ssh_failures(lab):
    lab.login()
    source = lab.state['nodes']['a']
    for label, mutation in [('错误 SSH 端口', {'ssh_port': 1}), ('不存在的 SSH 账号', {'ssh_user': 'missing-lab-user'})]:
        with lab.case('ssh-partial', label):
            lab.manager_api('PUT', '/servers/%s' % source['server_id'], json=mutation)
            try:
                response = lab.manager_api('POST', '/servers/%s/check' % source['server_id'])
                if response.json().get('ok'):
                    raise CheckFailure('错误 SSH 身份被错误接受')
            finally:
                lab.manager_api('PUT', '/servers/%s' % source['server_id'], json={'ssh_port': 22, 'ssh_user': 'root'})
    original_hosts = (lab.root / 'known_hosts').read_text()
    for label, changed in [('未信任主机密钥', ''), ('主机密钥变化', '\n'.join(
            line.replace(lab.state['addresses']['b'], lab.state['addresses']['a'])
            for line in original_hosts.splitlines() if line.startswith(lab.state['addresses']['b'])) + '\n')]:
        with lab.case('ssh-partial', label):
            try:
                (lab.root / 'known_hosts').write_text(changed)
                result = lab.manager_api('POST', '/servers/%s/check' % source['server_id']).json()
                if result.get('ok'):
                    raise CheckFailure('未通过信任校验的主机仍可执行管理操作')
            finally:
                (lab.root / 'known_hosts').write_text(original_hosts)
    original_key = (lab.root / 'id_rsa').read_bytes()
    with lab.case('ssh-partial', '无效私钥'):
        try:
            (lab.root / 'id_rsa').write_text('无效测试私钥\n')
            result = lab.manager_api('POST', '/servers/%s/check' % source['server_id'], expected=None)
            if result.status_code == 200 and result.json().get('ok'):
                raise CheckFailure('无效私钥被接受')
        finally:
            (lab.root / 'id_rsa').write_bytes(original_key)
    with lab.case('ssh-partial', 'SSH 用户无 Docker 权限'):
        lab.command('a', ['sh', '-ec', 'id lablimited >/dev/null 2>&1 || adduser -D lablimited; passwd -d lablimited >/dev/null; mkdir -p /home/lablimited/.ssh; cp /root/.ssh/authorized_keys /home/lablimited/.ssh/authorized_keys; chown -R lablimited:lablimited /home/lablimited/.ssh; chmod 700 /home/lablimited/.ssh; chmod 600 /home/lablimited/.ssh/authorized_keys'])
        with lab.ssh('a', username='lablimited') as client:
            _, stdout, _ = client.exec_command('docker info')
            if stdout.channel.recv_exit_status() == 0:
                raise CheckFailure('权限夹具错误：受限用户仍能管理 Docker')
        lab.manager_api('PUT', '/servers/%s' % source['server_id'], json={'ssh_user': 'lablimited'})
        try:
            task = lab.manager_api('POST', '/backups', json={'source_server_id': source['server_id']}).json()
            result = lab.poll_task('backup', task['id'])
            if result['status'] != 'failed' or not result.get('error_msg'):
                raise CheckFailure('没有 Docker 权限的备份未明确失败')
        finally:
            lab.manager_api('PUT', '/servers/%s' % source['server_id'], json={'ssh_user': 'root'})
    lab.wait_gitea('a')
    lab.note('ssh-partial', '主机信任、私钥、账号、端口和 Docker 权限', '通过', '当前拒绝路径保留原服务；目录写入权限故障仍需另测')


def address_failures(lab):
    lab.login()
    identifier = lab.state['nodes']['a']['server_id']
    original = lab.gitea_url('a')
    values = [('无协议 URL', original.removeprefix('http://'), True),
              ('错误 API 端口', original.rsplit(':', 1)[0] + ':1', False),
              ('错误 API 地址', 'http://10.231.254.254:3000', False),
              ('DNS 失败', 'http://missing.remote-lab.invalid:3000', False),
              ('HTTPS 协议校验', original.replace('http://', 'https://'), False)]
    for label, url, expected in values:
        with lab.case('address-partial', label):
            lab.manager_api('PUT', '/servers/%s' % identifier, json={'gitea_url': url})
            try:
                result = lab.manager_api('POST', '/servers/%s/check' % identifier).json()
                if bool(result.get('ok')) != expected:
                    raise CheckFailure('地址校验结果不符')
            finally:
                lab.manager_api('PUT', '/servers/%s' % identifier, json={'gitea_url': original})
    invalid_certificate(lab)
    lab.wait_gitea('a')


def invalid_certificate(lab):
    """真实 TLS 端点返回有效版本信息，防止把普通协议错误冒充证书拒绝。"""
    import requests
    from .network import control
    node, port = 'a', 3443
    identifier = lab.state['nodes'][node]['server_id']
    original = lab.gitea_url(node)
    directory = '/lab/tls-' + str(time.time_ns())
    with lab.case('address-partial', '跨网段 HTTPS 不信任证书明确拒绝'):
        lab.command(node, ['mkdir', '-m', '700', directory])
        lab.command(node, ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-subj', '/CN=remote-lab.invalid', '-addext', 'subjectAltName=IP:' + lab.state['addresses'][node],
            '-keyout', directory + '/key.pem', '-out', directory + '/cert.pem'])
        lab.command(node, ['chmod', '600', directory + '/key.pem'])
        script = '''"""隔离证书验证接收器，不记录凭据或转发请求。"""
import http.server, ssl
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"version":"1.27.3-remote-lab"}'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
server = http.server.HTTPServer(('0.0.0.0', 3443), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(__file__.replace('server.py','cert.pem'), __file__.replace('server.py','key.pem'))
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
'''
        lab.put(node, directory + '/server.py', script)
        pid = int(lab.command(node, ['sh', '-ec', 'python3 "$1" > "$2" 2>&1 < /dev/null & echo $!',
            'sh', directory + '/server.py', directory + '/server.log'])[1])
        cert = lab.root / ('tls-cert-' + str(time.time_ns()) + '.pem')
        cert.write_bytes(lab.command(node, ['cat', directory + '/cert.pem'])[1]); cert.chmod(0o600)
        enabled = False
        session = requests.Session(); session.trust_env = False
        try:
            control(lab, 'tls-port', enabled=True); enabled = True
            url = 'https://' + lab.state['addresses'][node] + ':' + str(port)
            deadline = time.monotonic() + 30
            while True:
                try:
                    positive = session.get(url + '/api/v1/version', verify=str(cert), timeout=3)
                    positive.raise_for_status()
                    if positive.json().get('version') != '1.27.3-remote-lab':
                        raise CheckFailure('TLS 夹具没有返回预期版本，不能判断证书拒绝')
                    break
                except requests.ConnectionError:
                    if time.monotonic() >= deadline:
                        raise CheckFailure('TLS 夹具未就绪，本次未命中证书检查')
                    time.sleep(0.2)
            lab.manager_api('PUT', '/servers/%s' % identifier, json={'gitea_url': url})
            result = lab.manager_api('POST', '/servers/%s/check' % identifier).json()
            write_json(lab.root / ('tls-rejection-' + str(time.time_ns()) + '.json'), {
                '说明': '客户端显式信任该测试证书时成功；Manager 使用其正常信任库必须拒绝，未绕过校验',
                '证书SHA256': hashlib.sha256(cert.read_bytes()).hexdigest(), '正向HTTP状态': positive.status_code,
                'Manager结果': result})
            if result.get('ok') or 'CERTIFICATE_VERIFY_FAILED' not in result.get('message', ''):
                raise CheckFailure('无效证书未被明确拒绝，或失败原因并非证书验证')
        finally:
            session.close()
            lab.manager_api('PUT', '/servers/%s' % identifier, json={'gitea_url': original})
            if enabled:
                control(lab, 'tls-port', enabled=False)
            lab.command(node, ['sh', '-ec',
                'if test -e /proc/"$1"/cmdline && tr "\\0" " " < /proc/"$1"/cmdline | grep -F -- "$2" >/dev/null; then kill "$1"; fi',
                'sh', str(pid), directory + '/server.py'])


def settings_migration(lab):
    """单独复现业务安全开关迁移；不算作三轮完整基线通过。"""
    lab.login()
    if not lab.state.get('settings_seeded'):
        raise CheckFailure('先执行新版 seed，实际确认源匿名访问限制生效')
    with lab.case('settings', '源登录访问限制在目标发布后保持'):
        source = lab.request('a', 'GET', '/labadmin/public-signin-proof', headers={'Authorization': ''}, expected=None)
        if '/user/login' not in source.url:
            raise CheckFailure('源实例的登录限制未生效，不能将该场景作为迁移缺陷')
        backup = lab.backup('a')
        task = lab.restore('b')
        if task['status'] != 'success':
            raise CheckFailure('安全配置迁移前恢复失败：' + task.get('error_msg', ''))
        target = lab.request('b', 'GET', '/labadmin/public-signin-proof', headers={'Authorization': ''}, expected=None)
        value = {'说明': '未携带 Cookie、账号或令牌的真实页面读取；源与目标使用相同仓库数据',
            '备份ID': backup['id'], '恢复ID': task['id'], '源HTTP状态': source.status_code,
            '源最终路径': source.url, '目标HTTP状态': target.status_code, '目标最终路径': target.url,
            '目标响应SHA256': hashlib.sha256(target.content).hexdigest(),
            '目标含仓库内容': target.status_code == 200 and 'public-signin-proof' in target.text and '/user/login' not in target.url}
        write_json(lab.root / ('settings-migration-' + str(task['id']) + '.json'), value)
        if value['目标含仓库内容']:
            raise CheckFailure('恢复成功发布后，源要求登录查看的内容可被匿名读取')
        if '/user/login' not in target.url:
            raise CheckFailure('目标返回其他错误页面，不能确认登录边界保持')


def soak(lab, hours):
    if hours != 24:
        lab.note('soak', '稳定性时间配置', '环境阻塞', '正式验收必须使用二十四小时，短时调试不能计为通过')
        raise CheckFailure('正式稳定性验收不得缩短时间')
    lab.login()
    with lab.case('soak-preflight', '二十四小时全程峰值空间预检'):
        try:
            lab.ensure_space(future_backups=12)
        except EnvironmentBlocked as exc:
            lab.note('soak', '二十四小时稳定性尚未启动', '环境阻塞', str(exc))
            raise
    from .helper_preservation import snapshot as helper_snapshot, compare as helper_compare
    retained_helpers = {node: helper_snapshot(lab, node) for node in ('a', 'b')}
    for rows in retained_helpers.values():
        helper_compare(rows, rows)
    write_json(lab.root / ('soak-helper-baseline-' + str(time.time_ns()) + '.json'), {
        '说明': '已停止的旧首败助手逐字段保留；本次新增助手不允许残留，活动助手会在写入前拒绝',
        '节点': retained_helpers})
    lab.state['baseline_dirty'] = '二十四小时持续写入开始，后续静态迁移前必须完整复验并重采基线'
    lab.save()
    start = time.monotonic()
    deadline = start + hours * 3600
    next_cycle, cycles = start, 0
    stop = threading.Event()
    acknowledgements = []
    interruptions = []
    journal = lab.root / ('soak-writes-' + str(time.time_ns()) + '.jsonl')

    def writer():
        from .metrics import continuity_issue
        sequence = 0
        previous_clock = None
        while not stop.is_set():
            current_clock = {'单调秒': time.monotonic(), '墙钟秒': time.time()}
            problem = continuity_issue(previous_clock, current_clock)
            if problem:
                interruptions.append(problem)
                with journal.open('a') as stream:
                    stream.write(json.dumps({'说明': problem, '状态': '环境阻塞', **current_clock}, ensure_ascii=False) + '\n')
                stop.set()
                break
            previous_clock = current_clock
            sequence += 1
            body = ('稳定性正文-%d-' % sequence).encode() * 4096
            endpoint = '/api/packages/labadmin/generic/soak-%s/%s/data.bin' % (int(start), sequence)
            record = {'编号': sequence, '开始单调秒': current_clock['单调秒'], '开始墙钟秒': current_clock['墙钟秒'], '包路径': endpoint,
                      'SHA256': hashlib.sha256(body).hexdigest(), '状态': '未确认'}
            try:
                lab.request('a', 'PUT', endpoint, data=body, timeout=5)
                issue = lab.state['nodes']['a']['seed']['issue']
                response = lab.api('a', 'POST', '/repos/labadmin/proof/issues/%s/comments' % issue,
                    json={'body': '稳定性确认写入：%d；%s' % (sequence, record['SHA256'])}, timeout=5).json()
                record.update(状态='已确认', 评论ID=response['id'], 完成单调秒=time.monotonic())
                acknowledgements.append(record)
            except Exception as exc:
                record['失败原因'] = lab.scrub(str(exc))
            with journal.open('a') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            stop.wait(30)

    worker = threading.Thread(target=writer, daemon=True)
    worker.start()
    try:
        with lab.case('soak', '二十四小时持续写入与串行恢复'):
            while time.monotonic() < deadline:
                if interruptions:
                    raise EnvironmentBlocked(interruptions[0])
                if time.monotonic() >= next_cycle:
                    cutoff = time.monotonic()
                    required = [row.copy() for row in acknowledgements if row['完成单调秒'] <= cutoff]
                    backup = lab.backup('a')
                    if interruptions:
                        raise EnvironmentBlocked(interruptions[0])
                    task = lab.restore('b')
                    if task['status'] != 'success':
                        raise CheckFailure('稳定性恢复失败')
                    oracle.verify_git(lab, 'b')
                    oracle.verify_lfs(lab, 'b')
                    oracle.verify_identity(lab, 'b')
                    for row in required:
                        downloaded = lab.request('b', 'GET', row['包路径']).content
                        if hashlib.sha256(downloaded).hexdigest() != row['SHA256']:
                            raise CheckFailure('备份前已确认的软件包写入丢失：%s' % row['编号'])
                        comment = lab.api('b', 'GET', '/repos/labadmin/proof/issues/comments/%s' % row['评论ID']).json()
                        if row['SHA256'] not in comment['body']:
                            raise CheckFailure('备份前已确认的评论与正文不一致')
                    for node in ('a', 'b'):
                        helper_compare(retained_helpers[node], helper_snapshot(lab, node))
                        if lab.command(node, ['docker', 'exec', 'gitea', 'test', '-e', '/data/.manager-restore-lock'], check=False)[0] == 0:
                            raise CheckFailure('完成后残留实例锁')
                    cycles += 1
                    next_cycle += 2 * 3600
                    lab.note('soak-cycle', '稳定性第%d次恢复' % cycles, '通过', 备份ID=backup['id'], 确认写入数=len(required))
                write_json(lab.root / 'soak-progress.json', {'说明': '实时进度不表示最终通过',
                    '已用秒': int(time.monotonic() - start), '已完成恢复次数': cycles,
                    '已确认写入数': len(acknowledgements), '目标小时': hours})
                stop.wait(min(30, max(0, deadline - time.monotonic())))
            if cycles < 12:
                raise CheckFailure('稳定性恢复次数不足十二次')
            if interruptions:
                raise EnvironmentBlocked(interruptions[0])
    finally:
        stop.set()
        worker.join(timeout=15)


def execute(lab, phase, hours):
    if phase == 'collaboration':
        from . import collaboration
        with lab.case('seed', '源端真实附件、Wiki历史、收藏关注和改名仓库'):
            if shutil.disk_usage(lab.root).free < 6 * 1024**3:
                raise EnvironmentBlocked('扩展协作仅用小型正文和克隆，仍要求六 GiB 余量，空间不足未操作')
            lab.state['baseline_dirty'] = '扩展协作准备和实际写入后需要重新采集'
            lab.save()
            collaboration.seed(lab, 'a')
            collaboration.verify(lab, 'a')
    elif phase == 'manager':
        from .manager_checks import run
        run(lab)
    elif phase == 'manager-permissions':
        from .manager_permissions import run
        run(lab)
    elif phase == 'ssh-keys':
        from . import identity_keys
        with lab.case('seed', '源端独立SSH身份、只读部署密钥及真实授权行为'):
            lab.state['baseline_dirty'] = '独立SSH准备和实际写入后需要重新采集'
            lab.save()
            identity_keys.seed(lab, 'a')
            identity_keys.verify(lab, 'a')
    elif phase == 'seed':
        seed(lab)
    elif phase == 'seed-integrations':
        from .integrations import seed as seed_integrations
        with lab.case('seed', '隔离集成源样本'):
            seed_integrations(lab)
    elif phase == 'integrations':
        from .integrations import migration
        migration(lab)
    elif phase == 'integrations-check':
        from .integrations import verify
        lab.login()
        with lab.case('integrations-partial', '已恢复目标的五类集成分别复验'):
            verify(lab, 'b')
    elif phase == 'validate-source':
        lab.login()
        lab.state['baseline_dirty'] = '源端完整真实读写复验已开始；只有随后完整采集新基线才能清除'
        lab.save()
        with lab.case('seed', '源端真实读写与业务夹具完整性'):
            if not verify_business(lab, 'a', '源端准备', preparation=True):
                raise CheckFailure('源端真实业务样本未通过，不能将相同夹具错误归因于备份恢复')
    elif phase == 'validate-target':
        lab.login()
        for endpoint in ('/backups', '/restore-tasks'):
            if any(row['status'] in ('running', 'pending') for row in lab.manager_api('GET', endpoint).json()):
                raise CheckFailure('已有活动备份恢复，不能开始目标真实业务复验')
        if lab.state['nodes']['b'].get('seed') != lab.state['nodes']['a'].get('seed'):
            raise CheckFailure('目标尚未采用源端样本身份，不能把旧目标当成恢复后验证')
        lab.state['baseline_dirty'] = '目标恢复后的真实读写复验已开始，后续静态迁移前需完整复验并重采'
        lab.save()
        with lab.case('restored-business-check', '当前已恢复目标的完整真实业务复验'):
            if not verify_business(lab, 'b', '当前恢复后的目标'):
                raise CheckFailure('恢复后真实业务仍有失败，逐项结果保留；不能将部分通过算作完整迁移通过')
    elif phase == 'checkpoint':
        from .checkpoint import prepare
        prepare(lab)
    elif phase == 'oracle':
        from .oracle_challenges import run
        run(lab)
    elif phase == 'process':
        from .process_faults import run
        run(lab)
    elif phase == 'database':
        from .database_faults import run
        run(lab)
    elif phase == 'archive':
        from .archive_faults import run
        run(lab)
    elif phase == 'ssh':
        ssh_failures(lab)
    elif phase == 'address':
        address_failures(lab)
    elif phase == 'storage':
        from .storage import failures
        failures(lab)
    elif phase == 'storage-preflight':
        from .storage import preflight_recheck
        preflight_recheck(lab)
    elif phase == 'storage-source':
        from .storage import source_failures
        source_failures(lab)
    elif phase == 'storage-manager':
        from .storage import manager_failures
        manager_failures(lab)
    elif phase == 'archive-paths':
        malformed_archives(lab)
    elif phase == 'unsupported':
        from .unsupported import failures
        failures(lab)
    elif phase == 'process-source-snapshot':
        from .process_faults import source_snapshot
        source_snapshot(lab)
    elif phase == 'manager-roles':
        from .manager_roles import run
        run(lab)
    elif phase in ('process-source-dump', 'process-source-transfer'):
        from .process_faults import source_snapshot
        source_snapshot(lab, stage=phase.rsplit('-', 1)[1])
    elif phase == 'database-source':
        from .database_faults import source_failures
        source_failures(lab)
    elif phase == 'database-import':
        from .database_faults import restore_interruption
        restore_interruption(lab, repeats=2)
    elif phase == 'archive-truncated':
        from .archive_faults import run
        run(lab, modes={'truncate'})
    elif phase == 'unsupported-pg':
        from .unsupported import pg_major
        pg_major(lab)
    elif phase == 'concurrent':
        from .extended import concurrent_operations
        concurrent_operations(lab)
    elif phase == 'schedule':
        from .extended import schedules
        schedules(lab)
    elif phase == 'post-publish':
        from .extended import post_publish
        post_publish(lab)
    elif phase == 'network':
        from .network import matrix_preflight, stable_profiles, disruptions
        matrix_preflight(lab)
        stable_profiles(lab)
        disruptions(lab)
    elif phase == 'network-stable':
        from .network import stable_profiles
        stable_profiles(lab)
    elif phase == 'checkpoint-full':
        from .checkpoint import full_roundtrip
        full_roundtrip(lab)
    elif phase == 'settings':
        settings_migration(lab)
    elif phase == 'baseline':
        baseline(lab)
    elif phase == 'normal':
        normal(lab)
    elif phase == 'offline':
        from .offline import verify
        verify(lab)
    elif phase == 'secrets':
        from .secrets import matrix
        matrix(lab)
    elif phase == 'fault':
        from .extended import concurrent_operations, schedules, post_publish
        malformed_archives(lab)
        ssh_failures(lab)
        address_failures(lab)
        concurrent_operations(lab)
        schedules(lab)
        post_publish(lab)
        from .storage import failures
        failures(lab)
        from .unsupported import failures as unsupported_failures
        unsupported_failures(lab)
        from .network import stable_profiles, disruptions
        stable_profiles(lab)
        disruptions(lab)
    elif phase == 'soak':
        soak(lab, hours)
    else:
        raise CheckFailure('不支持的测试阶段')
