"""经真实 API 触发并发、调度及发布后失败，不修改产品代码。"""
import concurrent.futures
import datetime
import time

import requests

from .driver import CheckFailure
from .network import control, wait_stage


def independent_request(lab, method, path, body):
    session = requests.Session(); session.trust_env = False
    session.cookies.update(lab.manager.cookies)
    session.headers.update(lab.manager.headers)
    return session.request(method, lab.manager_url + path, json=body, timeout=30)


def concurrent_operations(lab):
    lab.login()
    with lab.case('concurrent-preflight', '并发任务归档保留与目标恢复峰值空间'):
        lab.ensure_space(future_backups=1)
    with lab.case('concurrent-partial', '同一源并发备份互斥'):
        payload = {'source_server_id': lab.state['nodes']['a']['server_id']}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: independent_request(lab, 'POST', '/backups', payload), range(2)))
        if any(reply.status_code != 201 for reply in replies):
            raise CheckFailure('并发创建请求返回未约定状态')
        tasks = [lab.poll_task('backup', reply.json()['id']) for reply in replies]
        if sorted(item['status'] for item in tasks) != ['failed', 'success']:
            raise CheckFailure('同一源并发备份没有只允许一个执行')
        winner = next(item for item in tasks if item['status'] == 'success')
        lab.state.update(backup_id=winner['id'], backup_node='a'); lab.save()
        lab.wait_gitea('a')
    with lab.case('concurrent-partial', '同一目标并发恢复互斥'):
        payload = {'backup_id': lab.state['backup_id'], 'target_server_id': lab.state['nodes']['b']['server_id']}
        first = lab.manager_api('POST', '/restore', json=payload).json()
        wait_stage(lab, 'restore', first['id'], {'transfer_files', 'transfer_database'})
        second = lab.manager_api('POST', '/restore', json=payload).json()
        tasks = [lab.poll_task('restore', item['id']) for item in (first, second)]
        if sorted(item['status'] for item in tasks) != ['failed', 'success']:
            raise CheckFailure('同一目标恢复没有只允许一个执行')
        lab.adopt_identity('b', 'a')


def wait_schedule(lab, identifier, timeout=14400):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = next(row for row in lab.manager_api('GET', '/schedules').json() if row['id'] == identifier)
        if task.get('last_status') in ('failed', 'success'):
            return task
        time.sleep(2)
    raise CheckFailure('定时任务没有进入终态')


def schedules(lab):
    lab.login()
    with lab.case('schedule-preflight', '手工及定时两份归档保留与恢复峰值空间'):
        lab.ensure_space(future_backups=2)
    payload = {'name': '跨网段调度互斥验收', 'enabled': False,
               'source_server_id': lab.state['nodes']['a']['server_id'], 'target_ids': [lab.state['nodes']['b']['server_id']]}
    with lab.case('schedule-partial', '双 Worker 手工重复领取'):
        task = lab.manager_api('POST', '/schedules', json=payload).json()
        identifier = task['id']
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: independent_request(lab, 'POST', '/schedules/%s/run' % identifier, {}), range(2)))
        if sorted(reply.status_code for reply in replies) != [200, 400]:
            raise CheckFailure('重复领取未返回一个成功和一个拒绝')
        if wait_schedule(lab, identifier)['last_status'] != 'success':
            raise CheckFailure('完整调度未成功')
        lab.adopt_identity('b', 'a')
        logs = lab.manager_api('GET', '/schedules/%s/logs' % identifier).json()
        if len(logs) != 1 or len(logs[0].get('steps', [])) != 2:
            raise CheckFailure('调度日志数量或备份恢复步骤不正确')
    with lab.case('schedule-partial', '真实调度线程到点领取'):
        due = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=2)
        payload.update(name='真实到点调度', enabled=True, schedule_hour=due.hour, schedule_minute=due.minute)
        task = lab.manager_api('POST', '/schedules', json=payload).json()
        try:
            if wait_schedule(lab, task['id'])['last_status'] != 'success':
                raise CheckFailure('到点调度失败')
            lab.adopt_identity('b', 'a')
            logs = lab.manager_api('GET', '/schedules/%s/logs' % task['id']).json()
            if len(logs) != 1:
                raise CheckFailure('两个 Worker 重复执行了到点任务')
        finally:
            lab.manager_api('PUT', '/schedules/%s' % task['id'], json={'enabled': False})


def post_publish(lab):
    lab.login()
    with lab.case('post-publish-preflight', '发布后保护所需归档保留与回退峰值空间'):
        lab.ensure_space(future_backups=1)
    lab.backup('a')
    source = lab.state['nodes']['a']
    headers = {'Authorization': 'token ' + source['token']}
    with lab.case('post_publish-partial', '发布后健康确认失败不得吞掉新增业务'):
        control(lab, 'health-block', node='b', enabled=True)
        try:
            task = lab.manager_api('POST', '/restore', json={'backup_id': lab.state['backup_id'],
                'target_server_id': lab.state['nodes']['b']['server_id']}).json()
            wait_stage(lab, 'restore', task['id'], {'publish'})
            lab.adopt_identity('b', 'a')
            lab.wait_gitea('b')
            name = 'after-publish-' + str(task['id'])
            lab.api('b', 'POST', '/user/repos', headers=headers, json={'name': name, 'private': True, 'auto_init': True})
            package = '/api/packages/labadmin/generic/' + name + '/1.0.0/new.bin'
            lab.request('b', 'PUT', package, headers=headers, data='开放后的新数据'.encode())
            result = lab.poll_task('restore', task['id'], timeout=900)
            if result['status'] != 'failed' or '保留当前数据' not in result.get('error_msg', ''):
                raise CheckFailure('没有观察到发布后的受控失败')
        finally:
            control(lab, 'health-block', node='b', enabled=False)
        for _ in range(2):
            response = lab.manager_api('POST', '/restore-tasks/%s/recover' % task['id'], expected=(200, 409))
            lab.api('b', 'GET', '/repos/labadmin/' + name, headers=headers)
            if lab.request('b', 'GET', package, headers=headers).content != '开放后的新数据'.encode():
                raise CheckFailure('重复接管覆盖了发布后新增软件包')
