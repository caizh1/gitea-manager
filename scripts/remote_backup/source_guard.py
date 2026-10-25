"""源导出中断的严格归属保护；只接管本轮已观察到的同一助手。"""
from datetime import datetime, timezone
import json
import re
import shlex
import time

from .errors import CheckFailure
from . import helper_preservation
from .network import control


def require(condition, message):
    if not condition:
        raise CheckFailure(message)


def timestamp(value):
    require(isinstance(value, str), '缺少实际时间，拒绝关联事务')
    # Docker 纳秒只截到微秒；不改变实际秒数或时区。
    value = re.sub(r'(\.\d{6})\d+(?=Z|[+-])', r'\1', value)
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()


def identity(raw):
    require(re.fullmatch(r'[a-f0-9]{64}', raw.get('Id', '')), '容器不是完整身份')
    require(re.fullmatch(r'sha256:[a-f0-9]{64}', raw.get('Image', '')), '容器镜像身份无效')
    mounts = [{key: row.get(key) for key in ('Type', 'Name', 'Source', 'Destination', 'RW', 'Propagation')}
              for row in raw.get('Mounts', [])]
    require(mounts and len({row['Destination'] for row in mounts}) == len(mounts), '持久挂载缺失或重复')
    return {'容器': raw['Id'], '镜像': raw['Image'], '名称': raw['Name'],
            '创建时间': raw['Created'], '挂载': sorted(mounts, key=lambda row: row['Destination']),
            '重启次数': raw['RestartCount'], '状态': raw['State'],
            '来源卷': raw['HostConfig'].get('VolumesFrom') or [],
            '事务标记': (raw['Config'].get('Labels') or {}).get('gitea.manager.operation')}


def inspect(lab, name):
    raw = json.loads(lab.command('a', ['docker', 'inspect', name])[1])
    require(len(raw) == 1, '容器身份查询不唯一')
    return identity(raw[0])


def same_identity(original, current):
    keys = ('容器', '镜像', '名称', '创建时间', '挂载', '重启次数')
    require(all(original[key] == current[key] for key in keys), '原容器、镜像或挂载发生变化，拒绝接管')


def dump_processes(processes, marker):
    """只识别固定原命令；Rosetta 前缀不能把 shell 正文变成实际执行。"""
    expected = ['/usr/local/bin/gitea', 'dump', '-c', '/data/gitea/conf/app.ini',
                '--tempdir', '/tmp', '-f', '/tmp/' + marker + '.zip']
    matches = []
    for line in processes.splitlines()[1:]:
        row = line.strip().split(None, 1)
        if len(row) != 2 or not row[0].isdigit():
            continue
        try:
            args = shlex.split(row[1])
        except ValueError:
            continue
        if args[:4] == ['{gitea}', '/run/rosetta/rosetta', '/usr/local/bin/gitea', '/usr/local/bin/gitea']:
            args = args[3:]
        if args == expected:
            matches.append({'进程ID': int(row[0]), '参数': shlex.join(expected), '原始进程参数': row[1]})
    return matches


def validate_binding(task, diagnostic, source_id, original, helper, old_helpers, processes):
    """纯判定：诊断、源、唯一新助手和实际 dump 参数必须同时匹配。"""
    require(type(task.get('id')) is int and task['id'] > 0, '备份编号无效')
    require(task.get('source_server_id') == source_id and task.get('status') == 'running'
            and task.get('progress_stage') == 'dump', '本轮源导出任务已经变化')
    require(re.fullmatch(r'\d{8}T\d{6}Z-[a-f0-9]{8}', task.get('diagnostic_id') or ''), '诊断编号无效')
    require(diagnostic['任务ID'] == task['id'] and diagnostic['源ID'] == source_id
            and diagnostic['诊断编号'] == task['diagnostic_id'], '持久任务与诊断关联不一致')
    association = diagnostic['业务关联']
    require(association['业务编号'] == task['id'] and association['业务类型'] == '备份'
            and association['操作编号'] == task['diagnostic_id'], '诊断业务关联不匹配')
    status = diagnostic['诊断状态']
    require(status['状态'] == '运行中' and diagnostic['原诊断进程存活'], '实际备份执行进程已经退出')
    require(helper['容器'] not in old_helpers, '拒绝关联旧助手')
    marker = helper['事务标记']
    require(isinstance(marker, str) and re.fullmatch(r'manager-backup-[a-f0-9]{32}', marker), '助手事务标记无效')
    require(helper['镜像'] == original['镜像'] and helper['挂载'] == original['挂载'], '助手不属于原源挂载或镜像')
    require(helper['来源卷'] in ([original['容器']], [original['容器'] + ':rw']), '助手不是原源的完整 volumes_from')
    require(helper['状态']['Running'] and not helper['状态']['Paused']
            and not helper['状态']['Restarting'], '助手没有实际运行')
    require(timestamp(helper['创建时间']) >= max(timestamp(task['started_at']), float(status['开始时间']))
            and timestamp(helper['创建时间']) <= diagnostic['采集时间'], '助手创建时间不属于本次任务')
    matches = dump_processes(processes, marker)
    require(len(matches) == 1, '没有观察到本次标记对应的唯一真实 gitea dump 进程')
    return {'助手': helper, '导出进程': matches[0],
            '任务ID': task['id'], '诊断编号': task['diagnostic_id'], '持久诊断': diagnostic}


def validate_takeover(bound, helper, current_helpers, old_helpers, diagnostic):
    """旧助手保持原状态；接管对象必须就是注入前实际观察到的完整身份。"""
    require({key: current_helpers[key] for key in old_helpers if key in current_helpers} == old_helpers,
            '旧助手状态变化，拒绝接管')
    added = set(current_helpers) - set(old_helpers)
    require(added == {bound['助手']['容器']}, '接管助手不是本轮实际观察到的唯一完整身份')
    same_identity(bound['助手'], helper)
    require(helper['事务标记'] == bound['助手']['事务标记']
            and helper['来源卷'] == bound['助手']['来源卷'], '助手事务或来源卷发生变化')
    before = bound['持久诊断']
    require(diagnostic['任务ID'] == bound['任务ID'] and diagnostic['诊断编号'] == bound['诊断编号'],
            '接管时任务或诊断发生变化')
    require(diagnostic['后端容器'] == before['后端容器'] and diagnostic['后端镜像'] == before['后端镜像']
            and timestamp(diagnostic['后端启动时间']) > timestamp(before['后端启动时间'])
            and not diagnostic['原诊断进程存活'], '不能证明同一后端已重启且原备份进程退出')


def diagnostic(lab, identifier):
    return control(lab, 'backup-identity', backup_id=identifier)['证据']


def own_task(lab, identifier):
    backups = lab.manager_api('GET', '/backups').json()
    restores = lab.manager_api('GET', '/restore-tasks').json()
    require(not any(row['status'] in ('running', 'pending') and row['id'] != identifier for row in backups)
            and not any(row['status'] in ('running', 'pending') for row in restores), '出现其他活动业务，拒绝注入或接管')
    rows = [row for row in backups if row['id'] == identifier]
    require(len(rows) == 1, '本轮备份记录不唯一')
    return rows[0]


def run(lab):
    """一轮真实 dump 中断；保留首败，已确认归属后才显式人工收尾。"""
    lab.login()
    proof = {'说明': '只验证本轮源实际导出中断，人工接管不计自动恢复或生产RTO'}
    path = lab.root / ('source-dump-guarded-' + str(time.time_ns()) + '.json')
    def save(stage):
        proof['记录阶段'] = stage
        write_json(path, proof)
    with lab.case('process-partial', '严格绑定源实际 dump 后强杀 Manager，保留现场并人工接管'):
        # 整个单阶段保留一份可能完整的新归档；既有 previous 现场不删除。
        lab.ensure_space(future_backups=1)
        from . import oracle, manager_canaries
        from .scenarios import write_json
        for endpoint in ('/backups', '/restore-tasks'):
            require(not any(row['status'] in ('running', 'pending') for row in lab.manager_api('GET', endpoint).json()),
                    '已有活动备份恢复，尚未创建任务')
        for name in ('network-profile-active.json', 'network-actions-observer-active.json',
                     'database-import-observer-active.json', 'manager-storage-original.json', 'manager-role-tls-active.json'):
            require(not (lab.root / name).exists(), '有待处理活动标记，尚未创建任务')
        originals = {name: inspect(lab, name) for name in ('gitea', 'postgres')}
        require(all(row['状态']['Running'] for row in originals.values()), '原源或PG没有运行')
        old_helpers = helper_preservation.snapshot(lab, 'a')
        helper_preservation.compare(old_helpers, old_helpers)
        old_b = helper_preservation.snapshot(lab, 'b')
        helper_preservation.compare(old_b, old_b)
        require(lab.command('a', ['docker', 'exec', originals['gitea']['容器'], 'sh', '-ec',
            'test ! -e /data/.manager-restore-lock && test ! -L /data/.manager-restore-lock'],
            check=False)[0] == 0, '原源已有锁、悬空链接或检查失败')
        manager_canaries.verify(lab)
        before = oracle.snapshot(lab, 'a')
        lab.ensure_space(future_backups=1)
        proof.update({'原容器': originals, '原助手': old_helpers, '原目标助手': old_b,
                      '之前快照': before['证据文件']})
        save('准入与完整源快照完成')
        bound = None
        requested = False
        failure = None
        task = None
        with lab.observe('source-dump-guarded'):
            try:
                task = lab.manager_api('POST', '/backups', json={
                    'source_server_id': lab.state['nodes']['a']['server_id']}).json()
                proof['任务ID'] = task['id']; save('真实备份已提交，不重放POST')
                deadline = time.monotonic() + 3600
                while time.monotonic() < deadline:
                    current = own_task(lab, task['id'])
                    require(current['status'] == 'running', '备份在真实导出观察之前结束，未注入')
                    if current.get('progress_stage') == 'dump':
                        helpers = helper_preservation.snapshot(lab, 'a')
                        require({key: helpers.get(key) for key in old_helpers} == old_helpers, '旧助手发生变化')
                        added = set(helpers) - set(old_helpers)
                        require(len(added) == 1, '导出阶段没有唯一新助手，未注入')
                        helper = inspect(lab, next(iter(added)))
                        processes = lab.command('a', ['docker', 'top', helper['容器'], '-eo', 'pid,args'])[1].decode()
                        # 阶段字段先于实际进程写入；短暂等待进程出现，不按阶段名推断命中。
                        if '/usr/local/bin/gitea dump ' in processes:
                            proof['最近实际导出进程窗口'] = {'助手': helper, '进程': processes}
                            save('保存实际导出原始窗口，尚未注入')
                            current = own_task(lab, task['id'])
                            detail = diagnostic(lab, task['id'])
                            for name in originals:
                                actual = inspect(lab, name); same_identity(originals[name], actual)
                                require(actual['状态']['Running'] == (name == 'postgres'), '源或PG运行状态不符合实际停源导出')
                            bound = validate_binding(current, detail, lab.state['nodes']['a']['server_id'],
                                originals['gitea'], helper, old_helpers, processes)
                            # 诊断读取后再次观察同一实际进程，避免以已经结束的窗口注入。
                            again = lab.command('a', ['docker', 'top', helper['容器'], '-eo', 'pid,args'])[1].decode()
                            proof['第二次实际导出进程窗口'] = again
                            save('保存第二次实际导出原始窗口，尚未注入')
                            second = validate_binding(own_task(lab, task['id']), detail, lab.state['nodes']['a']['server_id'],
                                originals['gitea'], inspect(lab, helper['容器']), old_helpers, again)
                            require(second['导出进程'] == bound['导出进程'], '两次读取不是同一实际导出进程，未注入')
                            proof['注入前严格关联'] = bound; save('完整身份及真实导出进程已二次核对')
                            requested = True
                            proof['强杀控制请求已准备'] = True; save('请求强杀前保存归属')
                            proof['强杀回执'] = control(lab, 'kill-manager', node='a')
                            save('同一Manager已实际重启')
                            lab.login()
                            started = time.monotonic()
                            observations = []
                            while time.monotonic() - started < 180:
                                current = own_task(lab, task['id'])
                                try:
                                    status = lab.api('a', 'GET', '/version', expected=None, timeout=2).status_code
                                except Exception as exc:
                                    status = type(exc).__name__
                                observations.append({'重启后单调秒': round(time.monotonic() - started, 3),
                                    '任务状态': current['status'], '阶段': current.get('progress_stage'), '源认证HTTP': status})
                                if current['status'] not in ('running', 'pending'):
                                    break
                                time.sleep(2)
                            proof['产品观察'] = observations
                            proof['产品观察后任务'] = own_task(lab, task['id'])
                            save('产品自动恢复观察结束，尚未人工接管')
                            if proof['产品观察后任务']['status'] == 'running':
                                raise CheckFailure('真实导出强杀后三分钟任务仍running；产品首败已保留，随后仅人工收尾')
                            break
                    time.sleep(0.25)
                else:
                    raise CheckFailure('真实导出观察超时，未注入，不推断产品终态')
            except BaseException as exc:
                failure = exc
                proof['首次异常'] = {'类型': type(exc).__name__, '说明': lab.scrub(str(exc))}
                save('保留首次异常')
            finally:
                if requested:
                    # 控制回执异常也先核对实际重启；不能用旧任务、标签格式或名称盲接管。
                    lab.login()
                    current = own_task(lab, task['id'])
                    detail = diagnostic(lab, task['id'])
                    for name in originals:
                        same_identity(originals[name], inspect(lab, name))
                    helpers = helper_preservation.snapshot(lab, 'a')
                    added = set(helpers) - set(old_helpers)
                    if added or current['status'] == 'running':
                        require(bound is not None and added == {bound['助手']['容器']}, '没有同一实际观察助手，拒绝接管')
                        helper = inspect(lab, bound['助手']['容器'])
                        validate_takeover(bound, helper, helpers, old_helpers, detail)
                        proof['接管前归属'] = {'助手': helper, '持久诊断': detail}; save('接管归属核对完成')
                        lab.command('a', ['docker', 'stop', helper['容器']])
                        stopped = inspect(lab, helper['容器'])
                        require(not stopped['状态']['Running'], '同一助手仍运行，拒绝启动源或解锁')
                        proof['保留助手'] = stopped; save('同一助手已停止并保留')
                        try:
                            if current['status'] == 'running':
                                proof['指定任务收尾'] = control(lab, 'finish-backup', backup_id=task['id'])
                        finally:
                            for name in originals:
                                same_identity(originals[name], inspect(lab, name))
                            require(inspect(lab, 'postgres')['状态']['Running'], '原PG已经停止，拒绝擅自启动')
                            if not inspect(lab, 'gitea')['状态']['Running']:
                                lab.command('a', ['docker', 'start', originals['gitea']['容器']])
                            lock = lab.command('a', ['docker', 'exec', originals['gitea']['容器'], 'test', '-d',
                                '/data/.manager-restore-lock'], check=False)
                            require(lock[0] in (0, 1), '无法核实源实例锁')
                            if lock[0] == 0:
                                lab.command('a', ['docker', 'exec', originals['gitea']['容器'],
                                    'rmdir', '/data/.manager-restore-lock'])
                    else:
                        helper_preservation.compare(old_helpers, helpers)
                        require(inspect(lab, 'gitea')['状态']['Running'], '无助手但源未恢复，拒绝未知接管')
                    lab.wait_gitea('a')
                    after = oracle.snapshot(lab, 'a')
                    proof['之后快照'] = after['证据文件']
                    proof['源差异'] = oracle.compare(before, after)
                    proof['接管后任务'] = lab.poll_task('backup', task['id'], timeout=5)
                    require(proof['接管后任务']['status'] not in ('running', 'pending'), '收尾后任务仍活动')
                    retained = helper_preservation.snapshot(lab, 'a')
                    require({key: retained.get(key) for key in old_helpers} == old_helpers, '旧停止助手状态改变')
                    require(set(retained) - set(old_helpers) <= {bound['助手']['容器']}, '出现其他助手残留')
                    helper_preservation.compare(old_b, helper_preservation.snapshot(lab, 'b'))
                    manager_canaries.verify(lab)
                    save('人工接管与完整源核对完成')
                    require(not proof['源差异'], '人工接管后源原始业务有差异，保留完整现场')
                elif task is not None:
                    # 观察未命中时不强杀、不接管，持续等待真实任务终态。
                    proof['未注入任务实际终态'] = lab.poll_task('backup', task['id'])
                    save('没有注入，原业务任务已独立等待')
            if failure is not None:
                raise failure
            lab.note('process-safe-recovery', '严格绑定源导出中断', '通过', '实际任务自行结束，独立全表核对见受限证据')
