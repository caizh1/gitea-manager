"""在已造数的隔离环境用真实 API 逐一验收快速恢复的备份点。"""
import argparse
import base64
from datetime import datetime
import hashlib
import json
import time

from remote_backup.driver import Lab
from remote_backup.performance import ARTIFACTS, check_target_readable, refs, save, verify_target_data


def utc(value):
    return datetime.fromisoformat(value.rstrip('Z'))


def backup_row(lab, backup_id):
    return next(row for row in lab.manager_api('GET', '/backups').json() if row['id'] == backup_id)


def restore_row(lab, restore_id):
    return next(row for row in lab.manager_api('GET', '/restore-tasks').json() if row['id'] == restore_id)


def wait_for(check, elapsed, label):
    deadline = time.monotonic() + elapsed
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(3)
    raise RuntimeError(label + '等待超时')


def target_ready(lab, target_id, backup_id):
    def poll():
        state = backup_row(lab, backup_id)['replicas'].get(str(target_id), {})
        return state if state.get('state') == 'ready' else None

    return wait_for(poll, 1800, '等待备机完整 ZIP 校验')


def create_point(lab, fixture, number):
    body = f'快速恢复备份点 {number}\n'.encode()
    path = '/repos/labadmin/large/contents/fast-restore-point.txt'
    current = lab.api('a', 'GET', path, expected=(200, 404))
    commit = {'content': base64.b64encode(body).decode(), 'message': body.decode().strip(), 'branch': 'main'}
    if current.status_code == 200:
        commit['sha'] = current.json()['sha']
    lab.api('a', 'PUT' if current.status_code == 200 else 'POST', path, json=commit, expected=(200, 201))
    lab.api('a', 'POST', '/repos/labadmin/large/issues', json={'title': body.decode().strip()})
    name = f'sample/restore-point-{number:02d}.bin'
    lab.put('a', ARTIFACTS + '/' + name, body)
    lab.command('a', ['chown', '1000:1000', ARTIFACTS + '/' + name])
    copy = dict(fixture)
    copy['制品'] = dict(fixture['制品'], **{name: hashlib.sha256(body).hexdigest()})
    copy['引用'] = sorted(refs(lab, 'a'))
    copy['问题标题'] = sorted(item['title'] for item in lab.api('a', 'GET', '/repos/labadmin/large/issues').json())
    return copy


def wait_backup(lab, backup_id):
    def poll():
        row = backup_row(lab, backup_id)
        if row['status'] in ('running', 'pending'):
            return None
        if row['status'] != 'success':
            raise RuntimeError('真实全量备份失败：' + lab.scrub(row['error_msg']))
        return row

    return wait_for(poll, 8 * 3600, '等待完整备份')


def finish_restore(lab, target, backup_id, fixture, restore_id=None, verified_at=None, required=True):
    started = time.monotonic()
    if restore_id is None:
        restore = lab.manager_api('POST', '/restore', json={'backup_id': backup_id, 'target_server_id': target}).json()
        restore_id = restore['id']

    def poll():
        row = restore_row(lab, restore_id)
        return row if row['status'] not in ('running', 'pending', 'verifying') else None

    restore = wait_for(poll, 3600, '等待备机提供服务')
    if restore['status'] != 'success':
        raise RuntimeError('真实恢复失败：' + lab.scrub(restore['error_msg']))
    check_target_readable(lab)
    elapsed = round(time.monotonic() - started, 3)
    steps = lab.manager_api('GET', f'/restore-tasks/{restore_id}/steps').json()
    ordinary = any(step['step_key'] == 'transfer_files' for step in steps)
    restore_clock = round((utc(restore['completed_at']) - utc(verified_at)).total_seconds(), 3) if verified_at else elapsed
    stopped = next(row['started_at'] for row in steps if row['step_key'] == 'stop_gitea')
    opened = next(row['completed_at'] for row in steps if row['step_key'] == 'publish')
    result = {'备份编号': backup_id, '恢复编号': restore_id, '本地快速恢复': not ordinary,
              '恢复秒': restore_clock, '目标停服秒': round((utc(opened) - utc(stopped)).total_seconds(), 3),
              '15分钟内': restore_clock <= 900}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if required and ordinary:
        raise RuntimeError('当前恢复通过普通传输完成，未满足快速恢复验收路径')
    verify_target_data(lab, fixture)
    result['独立复核'] = '通过'
    return result


def current_sample(lab, args):
    root = lab.root
    current = json.loads((root / 'performance-fixture.json').read_bytes())
    if args.backup_id:
        if args.configure:
            configure(lab, args)
        target_ready(lab, args.target_id, args.backup_id)
        receipt = finish_restore(lab, args.target_id, args.backup_id, current)
        save(lab, 'fast-restore-existing-result.json', receipt)
        return
    task = configure(lab, args)
    results, fixtures = [], []
    for number in range(1, args.copies + 1):
        fixture = create_point(lab, current, number)
        current = fixture
        started = time.monotonic()
        if number == args.copies:
            lab.manager_api('POST', f'/schedules/{task}/run')
            def scheduled():
                row = next(x for x in lab.manager_api('GET', '/schedules').json() if x['id'] == task)
                if row['last_status'] == 'failed':
                    raise RuntimeError('定时备份恢复失败：' + lab.scrub(row['last_log']))
                return row['current_backup_id']
            backup_id = wait_for(scheduled, 600, '等待定时任务生成备份')
        else:
            backup_id = lab.manager_api('POST', '/backups', json={'source_server_id': args.source_id}).json()['id']
        row = wait_backup(lab, backup_id)
        ready = target_ready(lab, args.target_id, backup_id)
        stages = lab.manager_api('GET', f'/backups/{backup_id}/steps').json()
        results.append({'备份编号': backup_id, '成品字节': row['file_size'],
                        '从备份请求到备机就绪秒': round(time.monotonic() - started, 3),
                        '从备份校验到预传就绪秒': round(max(0, (utc(ready['updated_at']) - utc(row['completed_at'])).total_seconds()), 3),
                        '备份步骤': stages})
        fixtures.append((backup_id, fixture))
        save(lab, 'fast-restore-points.json', results)
    latest_id, latest_fixture = fixtures[-1]
    final_backup = backup_row(lab, latest_id)
    def find_automatic():
        return next((row['id'] for row in lab.manager_api('GET', '/restore-tasks').json()
                     if row['backup_id'] == latest_id and row['target_server_id'] == args.target_id), None)
    automatic = wait_for(find_automatic, 300, '等待定时恢复任务')
    automatic_result = finish_restore(lab, args.target_id, latest_id, latest_fixture, restore_id=automatic,
                                      verified_at=final_backup['completed_at'])
    count = min(7, len(fixtures))
    expected_ids = {backup_id for backup_id, _ in fixtures[-count:]}
    def retained():
        ready = {row['id'] for row in lab.manager_api('GET', '/backups').json()
                 if row['source_server_id'] == args.source_id and
                 row['replicas'].get(str(args.target_id), {}).get('state') == 'ready'}
        return ready == expected_ids
    if args.copies >= 7:
        wait_for(retained, 180, '等待最旧的缓存安全轮换')
    source_name = next(row['gitea_container'] for row in lab.manager_api('GET', '/servers').json()
                       if row['id'] == args.source_id)
    lab.command('a', ['docker', 'stop', source_name])
    manual = []
    try:
        for backup_id, fixture in fixtures[-count:]:
            item = finish_restore(lab, args.target_id, backup_id, fixture)
            manual.append(item)
            save(lab, 'fast-restore-seven-result.json', {'定时恢复': automatic_result, '手动恢复': manual})
    finally:
        lab.command('a', ['docker', 'start', source_name])
    if not all(item['15分钟内'] for item in [automatic_result, *manual]):
        raise RuntimeError('存在恢复耗时超过 15 分钟')


def configure(lab, args):
    return lab.manager_api('POST', '/schedules', json={'name': '固定备机保留最近七份验收', 'enabled': False,
        'source_server_id': args.source_id, 'target_ids': [args.target_id],
        'fast_restore_target_id': args.target_id, 'reuse_source_chunks': args.reuse}).json()['id']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--copies', type=int, default=8)
    parser.add_argument('--backup-id', type=int)
    parser.add_argument('--configure', action='store_true')
    parser.add_argument('--reuse', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.copies <= 8:
        parser.error('验收备份数应在一到八份')
    lab = Lab()
    lab.login()
    args.source_id = lab.state['nodes']['a']['server_id']
    args.target_id = lab.state['nodes']['b']['server_id']
    current_sample(lab, args)


if __name__ == '__main__':
    main()
