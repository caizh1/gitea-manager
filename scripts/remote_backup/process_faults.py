"""用真实阶段事件触发 Manager 强杀和独立引擎重启，保留悬挂任务证据。"""
import json
import re
import time

from .errors import CheckFailure
from .network import control, wait_stage


def one(lab, kind, stage, operation, context=None):
    from . import oracle
    node = 'a' if kind == 'backup' else 'b'
    before = oracle.snapshot(lab, node)
    if context is not None:
        context.update(之前快照=before, 中断已确认=False)
        context['原助手'] = lab.command(node, ['docker', 'ps', '--no-trunc', '-aq', '--filter', 'label=gitea.manager.operation'])[1].decode().splitlines()
    path = '/backups' if kind == 'backup' else '/restore'
    payload = {'source_server_id': lab.state['nodes']['a']['server_id']} if kind == 'backup' else {
        'backup_id': lab.state['backup_id'], 'target_server_id': lab.state['nodes']['b']['server_id']}
    task = lab.manager_api('POST', path, json=payload).json()
    if context is not None:
        context['任务ID'] = task['id']
    observed = wait_stage(lab, kind, task['id'], {stage})
    if kind == 'backup' and stage == 'dump' and context is not None:
        deadline = time.monotonic() + 30
        while True:
            helpers = set(lab.command(node, ['docker', 'ps', '--no-trunc', '-q', '--filter', 'label=gitea.manager.operation'])[1].decode().splitlines()) - set(context['原助手'])
            if len(helpers) != 1:
                raise CheckFailure('导出阶段没有唯一的新助手，未注入中断')
            helper = next(iter(helpers))
            processes = lab.command(node, ['docker', 'top', helper, '-eo', 'pid,args'])[1].decode()
            if '/usr/local/bin/gitea dump ' in processes:
                context['导出进程证据'] = {'容器': helper, '进程': processes}
                break
            current = next(row for row in lab.manager_api('GET', '/backups').json() if row['id'] == task['id'])
            if current.get('progress_stage') != 'dump' or time.monotonic() > deadline:
                raise CheckFailure('未观察到实际 gitea dump 进程，未注入中断')
            time.sleep(0.2)
    start = time.monotonic()
    control(lab, operation, node=node)
    if context is not None:
        context['中断已确认'] = True
    lab.login()
    endpoint = '/backups' if kind == 'backup' else '/restore-tasks'
    deadline = time.monotonic() + 180
    current = observed
    while time.monotonic() < deadline:
        current = next(item for item in lab.manager_api('GET', endpoint).json() if item['id'] == task['id'])
        if current['status'] != 'running':
            break
        time.sleep(2)
    containers = lab.command(node, ['docker', 'ps', '-a', '--format', '{{.Names}} {{.Status}}'], check=False)[1].decode()
    (lab.root / ('interrupted-%s-%s.json' % (kind, task['id']))).write_text(json.dumps({
        '说明': '实际命中阶段后中断进程', '阶段': observed['progress_stage'], '任务': current,
        '容器状态': containers, '恢复观察秒': time.monotonic() - start}, ensure_ascii=False, indent=2) + '\n')
    if current['status'] == 'running':
        raise CheckFailure('进程中断后三分钟任务仍显示 running；原始状态与容器证据已保留，任务 %s' % task['id'])
    if not current.get('error_msg') and current['status'] != 'success':
        raise CheckFailure('中断任务没有可见失败原因')
    if kind == 'restore' and current['status'] == 'failed':
        response = lab.manager_api('POST', '/restore-tasks/%s/recover' % task['id'], expected=None)
        if response.status_code not in (200, 409):
            raise CheckFailure('中断恢复没有提供有效接管结果')
    lab.wait_gitea(node, timeout=180)
    if current['status'] == 'failed' and stage != 'publish':
        changes = oracle.compare(before, oracle.snapshot(lab, node))
        if changes:
            raise CheckFailure('发布前中断后原业务未恢复：' + json.dumps(changes, ensure_ascii=False))


def source_snapshot(lab, stage='snapshot'):
    """旧单阶段入口只转入已补严格归属保护的真实导出测试。"""
    if stage not in ('snapshot', 'dump', 'transfer'):
        raise CheckFailure('源中断阶段不在已支持的只读备份阶段清单')
    if stage != 'dump':
        raise CheckFailure('旧源入口暂停：其他阶段仍须核实实际进程与完整接管归属')
    from .source_guard import run as guarded_source_dump
    return guarded_source_dump(lab)


def run(lab):
    raise CheckFailure('整组process尚缺整阶段保留预算及活动事务保护，禁止切换检查点或盲跑')
