"""固定数据集的全量备份、限速和恢复验收。"""
import hashlib
import json
import os
from pathlib import Path
import time

import requests

from .errors import CheckFailure

RATE = 2_000_000
REPO = '/lab/persist/gitea/git/repositories/labadmin/large.git'
ARTIFACTS = '/lab/persist/gitea/gitea/actions_artifacts'
GENERATOR = r'''
import hashlib, json, os, random, subprocess, sys
from pathlib import Path

repo, artifacts, spec = sys.argv[1:]
repo, artifacts = Path(repo), Path(artifacts)
git_bytes, artifact_bytes, small_count = map(int, spec.split(','))
rng = random.Random(20261121)

def blocks(size):
    while size:
        length = min(size, 1024*1024)
        yield rng.randbytes(length)
        size -= length

head = subprocess.check_output(['git','--git-dir',str(repo),'rev-parse','refs/heads/main']).strip().decode()
git = subprocess.Popen(['git','--git-dir',str(repo),'fast-import','--quiet','--force'], stdin=subprocess.PIPE)
def line(value):
    git.stdin.write(value.encode() + b'\n')
for index, offset in enumerate(range(0, git_bytes, 250_000_000), 1):
    size = min(250_000_000, git_bytes-offset)
    line('commit refs/heads/main')
    line('mark :'+str(index))
    line('committer Backup Lab <backup@lab.invalid> '+str(1700000000+index)+' +0000')
    line('data 6')
    line('样本')
    if index == 1:
        line('from '+head)
    line('M 100644 inline samples/object-%04d.bin' % index)
    line('data '+str(size))
    for block in blocks(size):
        git.stdin.write(block)
    line('')
    line('')
    print('已写入 Git 提交 %d' % index, file=sys.stderr, flush=True)
line('reset refs/heads/verification')
line('from refs/heads/main')
line('')
line('reset refs/tags/backup-baseline')
line('from refs/heads/main')
line('')
line('done')
git.stdin.close()
if git.wait():
    raise RuntimeError('真实 Git 对象生成失败')
for directory, dirs, files in os.walk(repo/'objects'):
    for name in dirs + files:
        os.chown(Path(directory)/name, 1000, 1000)
subprocess.check_call(['chown','-R','1000:1000',str(repo)])
index = {}
small_size = min(16_384, artifact_bytes // small_count // 2)
sizes = [small_size] * small_count
remaining = artifact_bytes - sum(sizes)
sizes.extend(min(500_000_000, remaining-offset) for offset in range(0, remaining, 500_000_000))
for n, size in enumerate(sizes):
    name = 'sample/%06d.bin' % n
    path = artifacts / name
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with path.open('xb') as handle:
        for block in blocks(size):
            handle.write(block)
            digest.update(block)
    index[name] = digest.hexdigest()
    if (n + 1) % 500 == 0:
        print('已生成 %d 件制品' % (n + 1), file=sys.stderr, flush=True)
subprocess.check_call(['chown','-R','1000:1000',str(artifacts)])
git_size = sum(p.stat().st_size for p in (repo/'objects').rglob('*') if p.is_file())
result = json.dumps({'Git对象字节':git_size,'制品字节':sum(sizes),'制品':index})
marker = Path('/lab/performance-seed-receipt.json')
temporary = marker.with_suffix('.partial')
temporary.write_text(result)
temporary.replace(marker)
print(result)
'''


def refs(lab, node):
    return lab.command(node, ['git', '--git-dir', REPO, 'show-ref'])[1].decode().splitlines()


def seed(lab, full):
    lab.api('a', 'POST', '/user/repos', json={'name': 'large', 'private': True, 'auto_init': True})
    lab.api('a', 'POST', '/repos/labadmin/large/issues', json={'title': '停写前的数据库验收记录'})
    release = lab.api('a', 'POST', '/repos/labadmin/large/releases',
                      json={'tag_name': 'fixture-assets', 'name': '验收发布', 'target_commitish': 'main'}).json()
    attachment = '附件原始正文'.encode()
    asset = lab.api('a', 'POST', f"/repos/labadmin/large/releases/{release['id']}/assets",
                    params={'name': 'proof.txt'}, files={'attachment': ('proof.txt', attachment)}).json()
    args = [20_000_000_000, 9_000_000_000, 10_000] if full else [48_000_000, 24_000_000, 120]
    lab.put('a', '/lab/performance-fixture.py', GENERATOR)
    report = json.loads(lab.command('a', ['python3', '/lab/performance-fixture.py', REPO, ARTIFACTS,
                                          ','.join(map(str, args))], timeout=2700)[1])
    finish_seed(lab, full, report, release['id'], asset['name'], attachment)


def finish_seed(lab, full, report, release_id, asset_name, attachment):
    sql = """INSERT INTO action_artifact (run_id, artifact_name, artifact_path,
             storage_path, status, expired_unix) VALUES
             (1, 'fixture', 'sample', 'sample/000000.bin', 2, 4102444800)"""
    if not full:
        sql += ", (1, 'fixture', 'missing', 'sample/removed.bin', 2, 4102444800)"
    lab.command('a', ['docker', 'exec', 'postgres', 'psql', '-U', 'gitea', '-d', 'gitea', '-v', 'ON_ERROR_STOP=1', '-c', sql])
    report['LFS'] = []
    for index in range(4 if full else 1):
        size = 250_000_000 if full else 2_000_000
        lfs = lab.root / f'fixture-lfs-{index}.bin'
        with lfs.open('wb') as stream:
            for offset in range(0, size, 1024 * 1024):
                stream.write(os.urandom(min(1024 * 1024, size - offset)))
        with lfs.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        lfs_response = lab.request('a', 'POST', '/labadmin/large.git/info/lfs/objects/batch',
                                   auth=('labadmin', lab.state['nodes']['a']['password']),
                                   headers={'Authorization': '', 'Accept': 'application/vnd.git-lfs+json',
                                            'Content-Type': 'application/vnd.git-lfs+json'},
                                   json={'operation': 'upload', 'transfers': ['basic'],
                                         'objects': [{'oid': digest, 'size': size}]}).json()['objects'][0]
        upload = lfs_response['actions']['upload']
        headers = {key: value for key, value in upload.get('header', {}).items()
                   if key.lower() not in ('transfer-encoding', 'content-length')}
        headers['Content-Length'] = str(size)
        with lfs.open('rb') as stream:
            response = lab.manager.put(upload['href'], data=stream, headers=headers, timeout=240)
            response.raise_for_status()
        report['LFS'].append({'摘要': digest, '字节': size})
        lfs.unlink()
    report['发布ID'] = release_id
    report['附件名'] = asset_name
    report['附件摘要'] = hashlib.sha256(attachment).hexdigest()
    report['引用'] = sorted(refs(lab, 'a'))
    save(lab, 'performance-fixture.json', report)


def save(lab, name, value):
    path = lab.root / name
    temporary = path.with_suffix('.partial')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def calibrate(lab):
    size = 24 * 1024 * 1024
    payload = os.urandom(size)
    rates = {}
    for node in ('a', 'b'):
        with lab.ssh(node) as client:
            begin = time.monotonic()
            if node == 'a':
                _, stdout, _ = client.exec_command(f'head -c {size} /dev/urandom', timeout=60)
                if len(stdout.read()) != size or stdout.channel.recv_exit_status() != 0:
                    raise CheckFailure('源站到 Manager 的限速校准未完整接收')
            else:
                stdin, stdout, stderr = client.exec_command('wc -c', timeout=60)
                stdin.channel.sendall(payload)
                stdin.close()
                if int(stdout.read()) != size or stdout.channel.recv_exit_status() != 0:
                    raise CheckFailure('Manager 到目标站的限速校准未完整接收：' + stderr.read().decode(errors='replace'))
            rates[node] = round(size / (time.monotonic() - begin))
    save(lab, 'performance-network.json', {'设定字节每秒': RATE, '实测字节每秒': rates})
    if not all(RATE * .80 <= rate <= RATE * 1.03 for rate in rates.values()):
        raise CheckFailure('实测吞吐偏离约定限速，拒绝开始正式计时')


def run(lab, full):
    lab.login()
    fixture = json.loads((lab.root / 'performance-fixture.json').read_text())
    schedule = lab.manager_api('POST', '/schedules', json={'name': '全量备份恢复计时验收', 'enabled': False,
                               'source_server_id': lab.state['nodes']['a']['server_id'],
                               'target_ids': [lab.state['nodes']['b']['server_id']]}).json()
    identifier = schedule['id']
    started = time.monotonic()
    lab.manager_api('POST', f'/schedules/{identifier}/run')
    post_snapshot = False
    backup_id = restore_id = None
    last_position = None
    phase_times = []
    source_health = []
    was_online = None
    elapsed = 0
    while elapsed < (16 * 3600 if full else 1200):
        row = next(item for item in lab.manager_api('GET', '/schedules').json() if item['id'] == identifier)
        elapsed = time.monotonic() - started
        backup_id = row['current_backup_id'] or backup_id
        restore_id = row['current_restore_task_id'] or restore_id
        position = (row['last_status'], row['progress_stage'])
        live = {'定时任务': identifier, '运行状态': row['last_status'], '阶段': row['progress_stage'],
                '已用秒': round(elapsed, 2), '已超过两小时': elapsed > 7200, '备份': backup_id, '恢复': restore_id,
                '已处理字节': row.get('progress_current'), '阶段总字节': row.get('progress_total'),
                '制品进度': row.get('artifact_progress')}
        if position != last_position:
            print(json.dumps(live, ensure_ascii=False), flush=True)
            last_position = position
            phase_times.append({'阶段': row['progress_stage'], '启动后秒': round(elapsed, 2)})
        try:
            online = lab.request('a', 'GET', '/api/v1/version', expected=None, timeout=.75).ok
        except requests.RequestException:
            online = False
        if was_online != online:
            source_health.append({'源站可访问': online, '启动后秒': round(time.monotonic() - started, 2)})
            was_online = online
        save(lab, 'performance-live.json', live)
        if not post_snapshot and row['progress_stage'] in ('transfer', 'assemble', 'manifest'):
            lab.api('a', 'POST', '/repos/labadmin/large/issues', json={'title': '源站恢复后新增，不属于备份点'})
            post_snapshot = True
        if row['last_status'] in ('success', 'failed', 'partial', 'partial_success'):
            break
        time.sleep(0.5 if not full else 2)
    logs = lab.manager_api('GET', f'/schedules/{identifier}/logs').json()
    if logs and not backup_id:
        backup_id = logs[0]['backup_id']
    backups = lab.manager_api('GET', '/backups').json()
    backup = next((item for item in backups if item['id'] == backup_id), None)
    restores = lab.manager_api('GET', '/restore-tasks').json()
    restore = next((item for item in restores if item['backup_id'] == backup_id), None)
    result = {**live, '源停写': backup and backup['source_stopped_at'],
              '源恢复': backup and backup['source_restored_at'], '备份就绪': backup and backup['completed_at'],
              '目标恢复完成': restore and restore['completed_at'], '目标恢复校验': restore and restore['verification_status'],
              '备份字节': backup and backup['file_size'], '两小时内完成': False}
    result.update({'阶段时间采样': phase_times, '源站健康采样': source_health})
    for name, path, record in [('备份步骤', '/backups', backup), ('恢复步骤', '/restore-tasks', restore)]:
        if record:
            result[name] = lab.manager_api('GET', f"{path}/{record['id']}/steps").json()
    save(lab, 'performance-result.json', result)
    if not restore or row['last_status'] != 'success' or restore['status'] != 'success' or restore['verification_status'] != 'success':
        raise CheckFailure('调度或恢复校验未通过，请检查阶段证据')
    if not full and (backup.get('actions_warning') or {}).get('examples') != ['sample/removed.bin']:
        raise CheckFailure('原本缺失的制品未按预期产生告警')
    check_target_readable(lab)
    elapsed = time.monotonic() - started
    result.update({'已用秒': round(elapsed, 2), '两小时内完成': elapsed <= 7200})
    verify_target_data(lab, fixture)
    result['内容复核'] = '通过'
    result['源站恢复后新增写入覆盖'] = post_snapshot
    save(lab, 'performance-result.json', result)


def check_target_readable(lab):
    lab.state['nodes']['b']['token'] = lab.state['nodes']['a']['token']
    lab.wait_gitea('b')
    lab.api('b', 'GET', '/repos/labadmin/large/contents/README.md')


def verify_target_data(lab, fixture):
    if sorted(refs(lab, 'b')) != fixture['引用']:
        raise CheckFailure('目标 Git 引用与源基准不一致')
    lab.command('b', ['git', '--git-dir', REPO, 'fsck', '--full', '--no-reflogs'], timeout=2700)
    items = lab.api('b', 'GET', '/repos/labadmin/large/issues').json()
    if {item['title'] for item in items} != set(fixture.get('问题标题', ['停写前的数据库验收记录'])):
        raise CheckFailure('目标业务内容与停写点不一致')
    asset = lab.api('b', 'GET', f"/repos/labadmin/large/releases/{fixture['发布ID']}/assets").json()[0]
    fetched = lab.request('b', 'GET', '/'+asset['browser_download_url'].split('/', 3)[3]).content
    if asset['name'] != fixture['附件名'] or hashlib.sha256(fetched).hexdigest() != fixture['附件摘要']:
        raise CheckFailure('发布附件与停写点不一致')
    for item in fixture['LFS']:
        digest = item['摘要']
        path = '/lab/persist/gitea/git/lfs/' + digest[:2] + '/' + digest[2:4] + '/' + digest[4:]
        actual = lab.command('b', ['sha256sum', path])[1].decode().split()[0]
        if actual != digest:
            raise CheckFailure('LFS 对象摘要不一致')
    lab.put('b', '/lab/performance-artifacts.json', json.dumps(fixture['制品']))
    lab.command('b', ['python3', '-c', """
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
expected = json.loads(Path(sys.argv[2]).read_bytes())
actual = {str(path.relative_to(root)) for path in root.rglob('*') if path.is_file()}
if actual != expected.keys():
    raise RuntimeError('恢复后制品集合与造数时不一致')
for name, digest in expected.items():
    with (root / name).open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
            raise RuntimeError('恢复后制品内容与造数时不一致')
""", ARTIFACTS, '/lab/performance-artifacts.json'], timeout=2700)


def retry(lab, backup_id):
    """保留正式首轮失败记录，使用同一备份点独立复验重收和恢复。"""
    lab.login()
    fixture = json.loads((lab.root / 'performance-fixture.json').read_text())
    previous = json.loads((lab.root / 'performance-result.json').read_text())
    if previous.get('备份') != backup_id or previous.get('运行状态') != 'failed':
        raise CheckFailure('重收需要对应的正式首轮失利证据')
    started = time.monotonic()
    attempt = lab.manager_api('POST', f'/backups/{backup_id}/retry-transfer').json()
    summary = {'原备份': backup_id, '重收备份': attempt['id'], '计时口径': '自发出重收请求到目标站可用'}
    save(lab, 'performance-retry-live.json', summary)
    backup = lab.poll_task('backup', attempt['id'], timeout=28800)
    summary['备份状态'] = backup['status']
    summary['重收秒'] = round(time.monotonic() - started, 2)
    summary['固定副本字节'] = backup['source_export_size']
    summary['备份字节'] = backup['file_size']
    summary['备份步骤'] = lab.manager_api('GET', f"/backups/{attempt['id']}/steps").json()
    save(lab, 'performance-retry-result.json', summary)
    if backup['status'] != 'success':
        raise CheckFailure('固定副本重收失败：' + backup.get('error_msg', ''))
    task = lab.manager_api('POST', '/restore', json={'backup_id': attempt['id'],
                           'target_server_id': lab.state['nodes']['b']['server_id']}).json()
    summary['恢复'] = task['id']
    save(lab, 'performance-retry-live.json', summary)
    restore = lab.poll_task('restore', task['id'], timeout=28800)
    summary['恢复状态'] = restore['status']
    summary['恢复验证'] = restore['verification_status']
    summary['恢复步骤'] = lab.manager_api('GET', f"/restore-tasks/{task['id']}/steps").json()
    save(lab, 'performance-retry-result.json', summary)
    if restore['status'] != 'success' or restore['verification_status'] != 'success':
        raise CheckFailure('目标恢复或验证失败：' + restore.get('error_msg', ''))
    check_target_readable(lab)
    summary['目标可用耗时秒'] = round(time.monotonic() - started, 2)
    verify_target_data(lab, fixture)
    summary['内容复核'] = '通过'
    save(lab, 'performance-retry-result.json', summary)
