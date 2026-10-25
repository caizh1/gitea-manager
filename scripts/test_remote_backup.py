"""验证验收器不会错判完整通过、选择重叠网段或操作其他实例。"""
import ipaddress
import copy
import json
from pathlib import Path
import tempfile
import unittest
import contextlib
from unittest.mock import patch, Mock

from remote_backup.catalog import CASES, verdict
from remote_backup.host import LABEL, assert_owned, route_network, select_networks, write_json
from remote_backup.payload import MultipartArchive, archive_budget
from remote_backup.checkpoint import name as checkpoint_name
from remote_backup.errors import CheckFailure
from remote_backup.host import freeze_driver_source, disruption_rules, manager_storage_mount
from remote_backup.metrics import collect, byte_count, continuity_issue, elapsed_seconds, probe_service
from remote_backup.audit_compare import recovery_append_only
from remote_backup.archive_faults import rewrite as rewrite_archive, synthetic_archive
from remote_backup.storage import parse_stop_events, preflight_result
import zipfile
import io
import hashlib
import ast
import shlex
from subprocess import CompletedProcess
from remote_backup.identity_keys import ssh_environment, write_denied
from remote_backup.manager_checks import denial
from remote_backup.host import browser_driver
from remote_backup.errors import EnvironmentBlocked


class AcceptanceSafetyTests(unittest.TestCase):
    def test_source_dump_recognizes_only_exact_native_or_fixed_rosetta_arguments(self):
        from remote_backup.source_guard import dump_processes
        *_, helper, old, processes = self.source_guard_fixture()
        direct = processes.splitlines()[1].split(None, 1)[1]
        wrapped = 'PID COMMAND\n42 {gitea} /run/rosetta/rosetta /usr/local/bin/gitea '+direct+'\n'
        self.assertEqual(len(dump_processes(wrapped, helper['事务标记'])), 1)
        self.assertEqual(len(dump_processes(processes, helper['事务标记'])), 1)
        for command in ('/bin/sh -c '+shlex.quote(direct),
                        '{gitea} /run/rosetta/rosetta /other/gitea '+direct,
                        '{other} /run/rosetta/rosetta /usr/local/bin/gitea '+direct,
                        direct+' --额外参数', direct.replace(helper['事务标记'], 'manager-backup-'+'0'*32)):
            with self.subTest(command=command):
                self.assertEqual(dump_processes('PID COMMAND\n42 '+command+'\n', helper['事务标记']), [])

    def test_source_diagnostic_reader_rejects_ambiguous_ids_before_container_access(self):
        from remote_backup.host import backup_identity
        with patch('remote_backup.host.service_id') as select:
            for value in (None, True, 0, -1, '37', 37.0):
                with self.subTest(value=value), self.assertRaises(RuntimeError):
                    backup_identity(Path('/受限证据'), value)
            select.assert_not_called()

    def source_guard_fixture(self):
        from remote_backup.source_guard import identity
        raw = {'Id': 'a'*64, 'Image': 'sha256:'+'b'*64, 'Name': '/gitea',
            'Created': '2026-10-23T17:00:00.123456789Z', 'RestartCount': 0,
            'State': {'Running': True, 'Paused': False, 'Restarting': False},
            'Mounts': [{'Type': 'bind', 'Source': '/lab/persist/gitea', 'Destination': '/data', 'RW': True}],
            'HostConfig': {'VolumesFrom': []}, 'Config': {'Labels': {}}}
        original = identity(raw)
        raw.update({'Id': 'c'*64, 'Name': '/助手', 'Created': '2026-10-23T17:10:02.123456789Z'})
        raw['HostConfig']['VolumesFrom'] = ['a'*64+':rw']
        marker = 'manager-backup-'+'d'*32
        raw['Config']['Labels']['gitea.manager.operation'] = marker
        helper = identity(raw)
        task = {'id': 38, 'source_server_id': 1, 'status': 'running', 'progress_stage': 'dump',
            'diagnostic_id': '20261023T171000Z-1234abcd', 'started_at': '2026-10-23T17:10:00'}
        from remote_backup.source_guard import timestamp
        detail = {'任务ID': 38, '源ID': 1, '诊断编号': task['diagnostic_id'],
            '诊断状态': {'状态': '运行中', '开始时间': timestamp(task['started_at'])+1},
            '业务关联': {'业务编号': 38, '业务类型': '备份', '操作编号': task['diagnostic_id']},
            '原诊断进程存活': True, '采集时间': timestamp(task['started_at'])+30,
            '后端容器': 'e'*64, '后端镜像': 'sha256:'+'f'*64, '后端启动时间': '2026-10-23T17:00:00Z'}
        processes = 'PID COMMAND\n42 /usr/local/bin/gitea dump -c /data/gitea/conf/app.ini --tempdir /tmp -f /tmp/'+marker+'.zip\n'
        old = {'1'*64: {'Status': 'exited', 'Running': False}}
        return task, detail, original, helper, old, processes

    def test_source_guard_binds_real_dump_arguments_to_task_and_persistent_diagnostic(self):
        from remote_backup.source_guard import validate_binding
        task, detail, original, helper, old, processes = self.source_guard_fixture()
        bound = validate_binding(task, detail, 1, original, helper, old, processes)
        self.assertEqual(bound['助手']['容器'], 'c'*64)
        for change in ('任务', '诊断', '业务关联', '源', '进程退出', '过早助手'):
            bad_task, bad_detail, bad_helper = copy.deepcopy(task), copy.deepcopy(detail), copy.deepcopy(helper)
            if change == '任务': bad_detail['任务ID'] = 20
            if change == '诊断': bad_detail['诊断编号'] = '20261023T171000Z-ffffffff'
            if change == '业务关联': bad_detail['业务关联']['业务编号'] = 20
            if change == '源': bad_task['source_server_id'] = 2
            if change == '进程退出': bad_detail['原诊断进程存活'] = False
            if change == '过早助手': bad_helper['创建时间'] = '2026-10-23T17:09:59Z'
            with self.subTest(change=change), self.assertRaises(CheckFailure):
                validate_binding(bad_task, bad_detail, 1, original, bad_helper, old, processes)

    def test_source_guard_rejects_same_marker_on_other_volume_or_other_dump_path(self):
        from remote_backup.source_guard import validate_binding
        task, detail, original, helper, old, processes = self.source_guard_fixture()
        for change in ('挂载', '镜像', '来源卷', '旧助手', '导出路径', '重复进程'):
            bad, top = copy.deepcopy(helper), processes
            if change == '挂载': bad['挂载'][0]['Source'] = '/lab/other'
            if change == '镜像': bad['镜像'] = 'sha256:'+'0'*64
            if change == '来源卷': bad['来源卷'] = ['0'*64+':rw']
            if change == '旧助手': bad['容器'] = next(iter(old))
            if change == '导出路径': top = processes.replace('/tmp/manager-backup-', '/tmp/other-')
            if change == '重复进程': top += processes.splitlines()[1]+'\n'
            with self.subTest(change=change), self.assertRaises(CheckFailure):
                validate_binding(task, detail, 1, original, bad, old, top)

    def test_source_takeover_requires_same_observed_full_helper_and_original_process_exit(self):
        from remote_backup.source_guard import validate_binding, validate_takeover
        task, detail, original, helper, old, processes = self.source_guard_fixture()
        bound = validate_binding(task, detail, 1, original, helper, old, processes)
        after = {**detail, '原诊断进程存活': False, '后端启动时间': '2026-10-23T17:11:00Z'}
        helpers = {**old, helper['容器']: {'Status': 'running'}}
        validate_takeover(bound, helper, helpers, old, after)
        for change in ('另一助手', '额外助手', '旧状态变化', '未重启', '进程仍活', '后端变更', '挂载变更'):
            bad, inventory, check = copy.deepcopy(helper), copy.deepcopy(helpers), copy.deepcopy(after)
            if change == '另一助手':
                inventory['0'*64] = inventory.pop(helper['容器'])
            if change == '额外助手': inventory['0'*64] = {'Status': 'running'}
            if change == '旧状态变化': inventory[next(iter(old))]['Running'] = True
            if change == '未重启': check['后端启动时间'] = detail['后端启动时间']
            if change == '进程仍活': check['原诊断进程存活'] = True
            if change == '后端变更': check['后端容器'] = '0'*64
            if change == '挂载变更': bad['挂载'][0]['Source'] = '/lab/other'
            with self.subTest(change=change), self.assertRaises(CheckFailure):
                validate_takeover(bound, bad, inventory, old, check)

    def test_process_group_and_unguarded_source_stages_stop_before_business_or_checkpoints(self):
        from remote_backup.process_faults import run, source_snapshot
        for function in (run, lambda lab: source_snapshot(lab, 'snapshot'), lambda lab: source_snapshot(lab, 'transfer')):
            lab = Mock()
            with self.assertRaises(CheckFailure): function(lab)
            self.assertFalse(lab.mock_calls)

    def test_source_dump_whole_stage_budget_blocks_before_post(self):
        from remote_backup.source_guard import run
        lab = Mock(); lab.root = Path('/受限证据'); lab.case.return_value = contextlib.nullcontext()
        lab.ensure_space.side_effect = EnvironmentBlocked('整阶段空间不足')
        with self.assertRaises(EnvironmentBlocked): run(lab)
        lab.ensure_space.assert_called_once_with(future_backups=1)
        lab.manager_api.assert_not_called()
        lab.command.assert_not_called()

    def test_task_polling_reauthenticates_once_without_replaying_business_writes(self):
        from remote_backup.polling import task_rows
        with tempfile.TemporaryDirectory() as directory:
            lab = Mock(); lab.root = Path(directory); lab.scrub = str
            denied = Mock(status_code=401, text='不得写入证据的响应正文')
            success = Mock(status_code=200); success.json.return_value = [{'id': 37, 'status': 'success'}]
            lab.manager_api.side_effect = [denied, success]
            rows, used = task_rows(lab, 'backup', 37)
            self.assertEqual(rows, [{'id': 37, 'status': 'success'}]); self.assertTrue(used)
            lab.login.assert_called_once_with()
            self.assertEqual(lab.manager_api.call_args_list, [
                unittest.mock.call('GET', '/backups', expected=None),
                unittest.mock.call('GET', '/backups', expected=None)])
            evidence = next(lab.root.glob('poll-authentication-*.json'))
            self.assertEqual(json.loads(evidence.read_text())['续读HTTP状态'], 200)
            self.assertNotIn('响应正文', evidence.read_text())
            lab.manager_api.side_effect = None; lab.manager_api.return_value = denied
            with self.assertRaisesRegex(CheckFailure, '返回 401'):
                task_rows(lab, 'backup', 37, refresh_used=used)
            lab.login.assert_called_once_with()

    def test_task_polling_keeps_denials_and_login_failures_visible(self):
        from remote_backup.polling import task_rows
        for code in (403, 500):
            lab = Mock(); lab.scrub = str
            lab.manager_api.return_value = Mock(status_code=code, text='读取被拒绝')
            with self.assertRaisesRegex(CheckFailure, '返回 '+str(code)):
                task_rows(lab, 'restore', 20)
            lab.login.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            lab = Mock(); lab.root = Path(directory)
            lab.manager_api.return_value = Mock(status_code=401)
            lab.login.side_effect = CheckFailure('不得写入证据的凭据正文')
            with self.assertRaises(CheckFailure):
                task_rows(lab, 'restore', 20)
            evidence = next(lab.root.glob('poll-authentication-*.json')).read_text()
            self.assertEqual(json.loads(evidence)['异常类型'], 'CheckFailure')
            self.assertNotIn('凭据正文', evidence)
            self.assertEqual(lab.manager_api.call_count, 1)

    def test_task_polling_reauthentication_does_not_reset_observation_deadline(self):
        source = Path(__file__).parent / 'remote_backup' / 'driver.py'
        tree = ast.parse(source.read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Lab')
        function = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == 'poll_task')
        clock = Mock(); initial_clock = iter([0, 0])
        clock.monotonic.side_effect = lambda: next(initial_clock, 11)
        clock.time_ns.return_value = 1
        scope = {'__package__': 'remote_backup', 'time': clock, 'json': json, 'CheckFailure': CheckFailure}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), scope)
        with tempfile.TemporaryDirectory() as directory:
            lab = Mock(); lab.root = Path(directory); lab.manager_url = 'http://backend:5000/api'
            with patch('remote_backup.polling.task_rows', return_value=([
                    {'id': 37, 'status': 'running', 'progress_stage': 'transfer'}], True)):
                with self.assertRaisesRegex(CheckFailure, '观察上限'):
                    scope['poll_task'](lab, 'backup', 37, timeout=10)
            saved = list((lab.root/'tasks').glob('*.json'))
            self.assertEqual(len(saved), 1)
            self.assertEqual(json.loads(saved[0].read_text())['status'], 'running')
            clock.sleep.assert_not_called()

    def test_phase_metrics_accept_explicit_monotonic_duration_without_replacing_bad_measurements(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'phase-runs').mkdir()
            path = root / 'phase-runs' / 'record.json'
            phase = {'阶段': '观察性恢复', '状态': '命令已结束',
                     '开始时间UTC': '2026-10-23T07:00:00+00:00',
                     '结束时间UTC': '2026-10-23T07:10:00+00:00', '单调耗时秒': 120.0}
            path.write_text(json.dumps(phase))
            result = collect(root)['阶段耗时'][0]
            self.assertEqual(result['单调计时秒'], 120.0)
            self.assertEqual(result['时钟差额秒'], 480.0)
            self.assertFalse(result['连续性能测量可用'])
            for value in (None, True, -1, float('nan')):
                path.write_text(json.dumps({**phase, '单调耗时秒': value}))
                with self.assertRaises(ValueError):
                    collect(root)
            path.write_text(json.dumps({**phase, '耗时秒': 121.0}))
            with self.assertRaises(ValueError):
                collect(root)

    def test_network_matrix_budgets_every_retained_archive_and_blocks_before_mutation(self):
        from remote_backup.network import retained_archives, matrix_preflight
        self.assertEqual(retained_archives(), 74)
        self.assertEqual(retained_archives(include_stable=False), 72)
        self.assertEqual(retained_archives(repeats=1), 38)
        lab = Mock(); lab.case.return_value = contextlib.nullcontext()
        lab.manager_api.return_value.json.return_value = [{'status':'running'}]
        with self.assertRaisesRegex(CheckFailure, '已有活动备份恢复'):
            matrix_preflight(lab)
        lab.ensure_space.assert_not_called(); lab.command.assert_not_called()
        lab.manager_api.return_value.json.return_value = []
        lab.ensure_space.side_effect = EnvironmentBlocked('整阶段资源不足')
        with self.assertRaises(EnvironmentBlocked):
            matrix_preflight(lab)
        lab.ensure_space.assert_called_once_with(future_backups=74)
        lab.command.assert_not_called()

    def test_network_queue_guard_rejects_children_and_missing_interface(self):
        from remote_backup.host import network_queues
        rows = [{'dev':'eth0','kind':'noqueue','handle':'0:','root':True,'options':{}}]
        self.assertEqual(network_queues(rows, ['eth0'])['eth0']['kind'], 'noqueue')
        for changed in (rows + [dict(rows[0], root=False)], [], [dict(rows[0], root=False)]):
            with self.assertRaises(RuntimeError):
                network_queues(changed, ['eth0'])

    def test_network_profile_requires_real_delay_rate_and_loss(self):
        from remote_backup.host import verify_network_profile
        actual = {'eth0':{'kind':'netem','options':{'delay':{'delay':0.1,'jitter':0.02},
                  'rate':{'rate':2500000},'loss-random':{'loss':0.005}}}}
        verify_network_profile(actual,'lossy')
        for section, key, value in (('delay','delay',0.01),('delay','jitter',0),('rate','rate',20000000),('loss-random','loss',0)):
            changed=copy.deepcopy(actual);changed['eth0']['options'][section][key]=value
            with self.assertRaises(RuntimeError):verify_network_profile(changed,'lossy')

    def test_network_profile_preserves_foreign_changes_and_rolls_back_only_own_partial_setup(self):
        from remote_backup.host import network_profile
        for mode in ('部分失败','所有者不同','实际配置变化'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root/'private.json').write_text(json.dumps({'networks':['10.231.10.0/24','10.231.20.0/24','10.231.30.0/24']}))
                rows = [{'dev':'eth'+str(i),'kind':'noqueue','handle':'0:','root':True,'options':{}} for i in range(3)]
                addresses = [{'ifname':'eth'+str(i),'addr_info':[{'local':'10.231.'+str((i+1)*10)+'.254'}]} for i in range(3)]
                mutations = []
                def fake_exec(_, service, args, **kwargs):
                    if args[0] == 'ip':
                        return CompletedProcess(args,0,json.dumps(addresses),'')
                    if args[:3] == ['tc','-j','qdisc']:
                        return CompletedProcess(args,0,json.dumps(rows),'')
                    interface = args[args.index('dev')+1]; index = int(interface[-1]); mutations.append(args)
                    if args[2] == 'replace':
                        if mode == '部分失败' and index == 1:
                            raise RuntimeError('模拟第二接口命令拒绝')
                        rows[index] = {'dev':interface,'kind':'netem','handle':'8001:','root':True,'options':{'delay':{'delay':0.05,'jitter':0.01},'rate':{'rate':12500000}}}
                    else:
                        rows[index] = {'dev':interface,'kind':'noqueue','handle':'0:','root':True,'options':{}}
                    return CompletedProcess(args,0,'','')
                with patch('remote_backup.host.service_id',return_value='本轮路由器'), \
                     patch('remote_backup.host.assert_owned',return_value={'Image':'固定镜像'}), \
                     patch('remote_backup.host.exec_service',side_effect=fake_exec):
                    owner = 'a'*32
                    if mode == '部分失败':
                        with self.assertRaisesRegex(RuntimeError,'模拟第二接口'):
                            network_profile(root,{'profile':'wan','profile_owner':owner})
                        self.assertTrue(all(row['kind']=='noqueue' for row in rows))
                        self.assertFalse((root/'network-profile-active.json').exists())
                        self.assertEqual([x[x.index('dev')+1] for x in mutations if x[2]=='del'],['eth0'])
                    else:
                        network_profile(root,{'profile':'wan','profile_owner':owner})
                        saved = (root/'network-profile-active.json').read_bytes(); count = len(mutations)
                        if mode == '实际配置变化':
                            rows[0]['options']['未知外部更改'] = True
                        with self.assertRaisesRegex(RuntimeError,'已有队列所有者'):
                            network_profile(root,{'profile':'normal','profile_owner':'b'*32 if mode=='所有者不同' else owner})
                        self.assertEqual(len(mutations),count)
                        self.assertEqual((root/'network-profile-active.json').read_bytes(),saved)

    def test_database_import_refuses_active_task_before_checkpoint_or_injection(self):
        source = Path(__file__).parent / 'remote_backup' / 'database_faults.py'
        tree = ast.parse(source.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'restore_interruption')
        function.body = [node for node in function.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
        checkpoints = Mock()
        scope = {'CheckFailure': CheckFailure, 'checkpoint': checkpoints}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), scope)
        lab = Mock()
        lab.case.return_value = contextlib.nullcontext()
        lab.manager_api.return_value.json.return_value = [{'status': 'running'}]
        with self.assertRaisesRegex(CheckFailure, '已有活动备份恢复'):
            scope['restore_interruption'](lab, repeats=2)
        checkpoints.restore.assert_not_called(); lab.command.assert_not_called()

    def test_database_import_requires_real_incoming_process_not_rollback_or_shell(self):
        from remote_backup.import_watch import incoming_process, observer_script
        marker = 'manager-restore-' + 'a' * 32
        process = '123 psql -X -v ON_ERROR_STOP=1 --single-transaction -U gitea -d gitea -f /tmp/' + marker + '/incoming.sql'
        self.assertEqual(incoming_process(process)['事务标记'], marker)
        for changed in (process.replace('incoming.sql', 'original.sql'),
                        process.replace('--single-transaction', ''),
                        process.replace('-d gitea', '-d postgres'),
                        process.replace('123 psql', '123 sh -c psql'),
                        process.replace(marker, 'manager-restore-未知标记'), 'PID COMMAND'):
            self.assertIsNone(incoming_process(changed))
        compile(observer_script(), '实际导入观察器', 'exec')

    def test_real_rosetta_psql_format_and_transaction_binding(self):
        from remote_backup.import_watch import incoming_process, journal_matches
        marker = 'manager-restore-' + 'a' * 32
        argv = ('123 {psql} /run/rosetta/rosetta /usr/lib/postgresql/16/bin/psql '
                '/usr/lib/postgresql/16/bin/psql -X -v ON_ERROR_STOP=1 --single-transaction '
                '-U gitea -d gitea -f /tmp/' + marker + '/incoming.sql')
        observed = incoming_process(argv)
        self.assertIsNotNone(observed)
        for altered in (argv.replace('incoming.sql', 'original.sql'),
                        argv.replace('/run/rosetta/rosetta', '/bin/sh'),
                        argv.replace('{psql}', '{sh}'),
                        argv.replace('123 {psql}', '123 sh -c {psql}'),
                        argv.replace('/usr/lib/postgresql/16/bin/psql', '/usr/lib/postgresql/16/bin/bash', 1)):
            self.assertIsNone(incoming_process(altered))
        journal = {'版本':1,'任务':16,'助手':'b'*64,'数据库容器':'c'*64,
                   '标识':marker,'阶段':'数据库修改','数据库暂存':'/tmp/'+marker,'数据库名称':'gitea'}
        self.assertTrue(journal_matches(journal, observed, 16, 'c'*64, 'b'*64))
        for key, value in (('任务',15),('助手','d'*64),('数据库容器','e'*64),('标识','其他事务'),
                           ('阶段','完成'),('数据库暂存','/tmp/其他事务'),('数据库名称','postgres')):
            altered = dict(journal); altered[key] = value
            self.assertFalse(journal_matches(altered, observed,16,'c'*64,'b'*64))

    def test_native_runtime_counter_does_not_hide_content_or_other_rows(self):
        from remote_backup.runtime_compare import native_runtime_transition
        before = [{'id': 'runtime-state', 'revision': 4, 'content': '相同的原生运行配置'},
                  {'id': '其他状态', 'revision': 7, 'content': '必须完整保留'}]
        after = copy.deepcopy(before); after[0]['revision'] = 6
        self.assertTrue(native_runtime_transition(before, after))
        for revision in (3, 5, 7, True):
            altered = copy.deepcopy(after); altered[0]['revision'] = revision
            self.assertFalse(native_runtime_transition(before, altered))
        altered = copy.deepcopy(after); altered[0]['content'] = '正文被改变'
        self.assertFalse(native_runtime_transition(before, altered))
        altered = copy.deepcopy(after); altered[1]['revision'] += 2
        self.assertFalse(native_runtime_transition(before, altered))
        self.assertFalse(native_runtime_transition(before, after[:1]))
        self.assertFalse(native_runtime_transition(before, after + [after[0]]))
        self.assertFalse(native_runtime_transition(before, before))

    def test_target_business_check_blocks_active_task_and_old_identity_before_writes(self):
        source = Path(__file__).parent / 'remote_backup' / 'scenarios.py'
        tree = ast.parse(source.read_text())
        selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'execute']
        verify = Mock()
        scope = {'CheckFailure': CheckFailure, 'verify_business': verify}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), scope)
        lab = Mock(state={'nodes': {'a': {'seed': {'身份': '源'}}, 'b': {'seed': {'身份': '旧目标'}}}})
        lab.manager_api.return_value.json.return_value = [{'status': 'running'}]
        with self.assertRaisesRegex(CheckFailure, '已有活动备份恢复'):
            scope['execute'](lab, 'validate-target', 24)
        lab.save.assert_not_called(); verify.assert_not_called()
        lab.manager_api.return_value.json.return_value = []
        with self.assertRaisesRegex(CheckFailure, '目标尚未采用源端样本身份'):
            scope['execute'](lab, 'validate-target', 24)
        lab.save.assert_not_called(); verify.assert_not_called()

    def test_soak_preserves_only_unchanged_stopped_helpers(self):
        from remote_backup.helper_preservation import compare
        stopped = {'Status': 'exited', 'Running': False, 'Paused': False,
                   'Restarting': False, 'ExitCode': 137, 'StartedAt': '原启动时间', 'FinishedAt': '原退出时间'}
        before = {'a' * 64: stopped}
        compare(before, copy.deepcopy(before))
        for changed in ({}, dict(before, **{'b' * 64: stopped}),
                        {'a' * 64: dict(stopped, StartedAt='重新启动时间')},
                        {'a' * 64: dict(stopped, ExitCode=0)},
                        {'a' * 64: dict(stopped, Status='running', Running=True)}):
            with self.assertRaisesRegex(CheckFailure, '新助手残留'):
                compare(before, changed)
        for changed in (dict(stopped, Status='running', Running=True),
                        dict(stopped, Paused=True), dict(stopped, Restarting=True)):
            with self.assertRaisesRegex(CheckFailure, '已有活动助手'):
                compare({'a' * 64: changed}, {'a' * 64: changed})

    def test_manager_permission_baseline_refuses_active_or_foreign_fixture(self):
        from remote_backup.manager_permissions import baseline
        with tempfile.TemporaryDirectory() as folder:
            lab = Mock(root=Path(folder), state={'manager_canaries': {}, 'manager_role_fixture': {
                'directory_id': 1, 'account_id': 2, 'name': '本轮专属目录'}})
            directory = {'id': 1, 'name': '本轮专属目录', 'enabled': False}
            account = {'id': 2, 'source': 'ldap', 'username': 'labldap', 'enabled': False,
                       'is_platform_admin': False, 'grants': []}
            for changed in ({'enabled': True}, {'is_platform_admin': True}, {'grants': [1]}):
                lab.manager_api.side_effect = [Mock(json=lambda: directory), Mock(json=lambda: {'items': [dict(account, **changed)]})]
                with self.assertRaisesRegex(CheckFailure, '拒绝建立基线'):
                    baseline(lab)
                lab.save.assert_not_called()
            lab.manager_api.side_effect = [Mock(json=lambda: dict(directory, name='其他目录')), Mock(json=lambda: {'items': [account]})]
            with self.assertRaisesRegex(CheckFailure, '不属于已登记夹具'):
                baseline(lab)
            self.assertEqual(list(Path(folder).iterdir()), [])

    @patch('remote_backup.manager_role_host.shutil.disk_usage', return_value=Mock(free=5 * 1024**3))
    def test_role_tls_space_rejection_precedes_container_access(self, _usage):
        from remote_backup.manager_role_host import configure
        with tempfile.TemporaryDirectory() as folder, patch('remote_backup.host.assert_owned') as inspect, \
             patch('remote_backup.host.run') as execute:
            with self.assertRaisesRegex(RuntimeError, '六 GiB'):
                configure(Path(folder), True)
            inspect.assert_not_called(); execute.assert_not_called()

    def test_role_tls_cleanup_refuses_changed_namespace(self):
        from remote_backup.manager_role_host import configure
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / 'manager-role-tls-active.json', {'容器': '测试容器', '镜像': '冻结镜像', '后端容器': '冻结后端'})
            with patch('remote_backup.host.assert_owned', return_value={'Image': '冻结镜像',
                    'HostConfig': {'NetworkMode': 'container:其他后端'}}), patch('remote_backup.host.run') as execute:
                with self.assertRaisesRegex(RuntimeError, '拒绝停止未知容器'):
                    configure(root, False)
                execute.assert_not_called()
                self.assertTrue((root / 'manager-role-tls-active.json').exists())

    def test_source_interrupt_rejects_unlisted_stage_before_login(self):
        source = Path(__file__).parent / 'remote_backup' / 'process_faults.py'
        tree = ast.parse(source.read_text())
        selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'source_snapshot']
        scope = {'CheckFailure': CheckFailure}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), scope)
        lab = Mock()
        with self.assertRaisesRegex(CheckFailure, '阶段不在'):
            scope['source_snapshot'](lab, stage='copy_files')
        lab.login.assert_not_called()
        lab.manager_api.assert_not_called()

    @patch('remote_backup.host.shutil.disk_usage', return_value=Mock(free=10 * 1024**3))
    def test_browser_cannot_access_wrong_port_binding_or_changed_image(self, _usage):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / 'private.json', {'http_port': 63493})
            write_json(root / 'images.json', {'frontend': {'摘要': 'frozen-ui'}, 'manager': {'摘要': 'frozen-api'}})
            for binding in ([], [{'HostIp': '0.0.0.0', 'HostPort': '63493'}],
                            [{'HostIp': '127.0.0.1', 'HostPort': '5000'}]):
                attrs = {'frontend': {'Image': 'frozen-ui', 'NetworkSettings': {'Ports': {'80/tcp': binding}}},
                         'backend': {'Image': 'frozen-api'}}
                with patch('remote_backup.host.service_id', side_effect=lambda root, name: name), \
                     patch('remote_backup.host.assert_owned', side_effect=lambda root, name: attrs[name]), \
                     patch('remote_backup.host.run') as execute:
                    with self.assertRaisesRegex(RuntimeError, '唯一回环端口'):
                        browser_driver(root)
                    execute.assert_not_called()
            attrs['frontend']['Image'] = 'different-ui'
            with patch('remote_backup.host.service_id', side_effect=lambda root, name: name), \
                 patch('remote_backup.host.assert_owned', side_effect=lambda root, name: attrs[name]), \
                 patch('remote_backup.host.run') as execute:
                with self.assertRaisesRegex(RuntimeError, '不匹配冻结镜像'):
                    browser_driver(root)
                execute.assert_not_called()

    @patch('remote_backup.host.shutil.disk_usage', return_value=Mock(free=5 * 1024**3))
    def test_browser_resource_rejection_precedes_container_or_browser_access(self, _usage):
        with tempfile.TemporaryDirectory() as folder, patch('remote_backup.host.assert_owned') as inspect, \
             patch('remote_backup.host.run') as execute:
            with self.assertRaisesRegex(RuntimeError, '六 GiB'):
                browser_driver(Path(folder))
            inspect.assert_not_called()
            execute.assert_not_called()

    def test_changed_source_fixture_blocks_normal_before_any_task_request(self):
        source = Path(__file__).parent / 'remote_backup' / 'scenarios.py'
        tree = ast.parse(source.read_text())
        selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'normal']
        scope = {'CheckFailure': CheckFailure}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), scope)
        lab = Mock(state={'baseline_dirty': '新增真实协作数据尚未采集'})
        with self.assertRaisesRegex(CheckFailure, '先重新执行 baseline'):
            scope['normal'](lab)
        lab.manager_api.assert_not_called()
        lab.backup.assert_not_called()
        lab.restore.assert_not_called()

    def test_manager_rejection_requires_exact_reason_and_json_status(self):
        response = Mock(status_code=401)
        response.json.return_value = {'error': '请先登录'}
        denial(response, 401, '请先登录')
        for value in ({'error': '其他限制'}, [], None):
            response.json.return_value = value
            with self.assertRaises(CheckFailure):
                denial(response, 401, '请先登录')
        response.json.return_value = {'error': '请先登录'}
        response.status_code = 503
        with self.assertRaises(CheckFailure):
            denial(response, 401, '请先登录')
        response.status_code = 401
        response.json.side_effect = ValueError('非 JSON 错误页')
        with self.assertRaises(CheckFailure):
            denial(response, 401, '请先登录')

    def test_ssh_identity_is_pinned_without_agent_or_global_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); keys = root / 'identity-keys'; keys.mkdir()
            key = keys / 'reader'; key.write_text('仅测试路径约束，无真实私钥')
            with patch.dict('os.environ', {'GIT_CONFIG_VALUE_0': '不允许继承的认证头'}):
                env = ssh_environment(root, key)
            command = shlex.split(env['GIT_SSH_COMMAND'])
            self.assertEqual(command[1:3], ['-F', '/dev/null'])
            self.assertIn('IdentityAgent=none', command)
            self.assertIn('StrictHostKeyChecking=yes', command)
            self.assertNotIn('GIT_CONFIG_VALUE_0', env)
            self.assertEqual(env['GIT_CONFIG_GLOBAL'], '/dev/null')
            link = keys / 'link'; link.symlink_to(key)
            with self.assertRaises(CheckFailure):
                ssh_environment(root, link)

    def test_ssh_readonly_rejection_requires_specific_server_permission_error(self):
        self.assertTrue(write_denied(CompletedProcess([], 1, b'', b'User permission denied for writing.')))
        self.assertTrue(write_denied(CompletedProcess([], 1, b'', b'Deploy Key is not authorized to write to repo.')))
        for code, message in ((0, b'User permission denied for writing.'), (1, b'Connection refused'),
                              (1, b'Host key verification failed'), (1, b'Protected branch hook declined')):
            self.assertFalse(write_denied(CompletedProcess([], code, b'', message)))

    def test_recovery_timestamp_exclusion_is_limited_to_unknown_rows(self):
        source = Path(__file__).parent / 'remote_backup' / 'oracle.py'
        tree = ast.parse(source.read_text())
        selected = [node for node in tree.body if (isinstance(node, ast.Assign)
            and any(isinstance(item, ast.Name) and item.id == 'VOLATILE_FIELDS' for item in node.targets))
            or (isinstance(node, ast.FunctionDef) and node.name in ('tables_query', 'collection_rules', 'compare'))]
        scope = {'CheckFailure': CheckFailure, 'DEPLOYMENT_CONFIG': set()}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), 'exec'), scope)
        query = scope['tables_query'](['repository', 'governance_repository_creation'])
        self.assertIn("CASE WHEN t.state='unknown' THEN to_jsonb(t) - 'next_attempt_unix' ELSE to_jsonb(t) END", query)
        self.assertNotIn('governance_repository_creation', scope['VOLATILE_FIELDS'])
        self.assertIn('FROM "repository" t', query)
        self.assertIn('FROM "governance_repository_creation" t', query)
        with self.assertRaises(CheckFailure):
            scope['tables_query']([])
        with self.assertRaisesRegex(CheckFailure, '采集规则不一致'):
            scope['compare']({'规则': {'旧版本': True}}, {'规则': scope['collection_rules']()})

    def test_stop_events_are_bound_to_real_container_and_task_window(self):
        raw = '\n'.join(json.dumps(row) for row in [
            {'Action': 'stop', 'Actor': {'ID': 'source'}, 'timeNano': 12_000_000_000},
            {'status': 'stop', 'id': 'source', 'timeNano': 13_000_000_000},
            {'Action': 'start', 'Actor': {'ID': 'source'}, 'timeNano': 14_000_000_000},
            {'Action': 'stop', 'Actor': {'ID': 'other'}, 'timeNano': 15_000_000_000},
            {'Action': 'stop', 'Actor': {'ID': 'source'}, 'timeNano': 9_000_000_000}]).encode()
        events = parse_stop_events(raw, 'source', 10, 16)
        self.assertEqual([row['纳秒'] for row in events], [12_000_000_000, 13_000_000_000])
        proof = {'源停容器事件': events, '任务区间源不可达样本数': 0, '任务区间源未确认样本数': 0, '任务区间样本数': 3}
        self.assertEqual(preflight_result(proof)[0], '失败')
        proof.update(源停容器事件=[], 任务区间源未确认样本数=3)
        with self.assertRaises(CheckFailure):
            preflight_result(proof)

    def test_probe_requires_auth_and_cannot_mistake_forbidden_for_outage(self):
        session = Mock()
        session.get.return_value.status_code = 403
        denied = probe_service(session, 'http://isolated-source', 'private-test-identity')
        self.assertIsNone(denied['可用'])
        self.assertEqual(denied['HTTP状态'], 403)
        self.assertEqual(session.get.call_args.kwargs['headers'], {'Authorization': 'token private-test-identity'})
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
        self.assertNotIn('private-test-identity', json.dumps(denied))
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {'version': '1.27.3'}
        self.assertTrue(probe_service(session, 'http://isolated-source', 'private-test-identity')['可用'])
        import requests
        session.get.side_effect = requests.ConnectionError('含凭据的异常不得落盘')
        disconnected = probe_service(session, 'http://isolated-source', 'private-test-identity')
        self.assertFalse(disconnected['可用'])
        self.assertNotIn('含凭据', json.dumps(disconnected, ensure_ascii=False))
        session.reset_mock()
        self.assertIsNone(probe_service(session, 'http://isolated-source', None)['可用'])
        session.get.assert_not_called()

    def test_anonymous_legacy_availability_cannot_be_used_for_downtime(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / 'availability').mkdir()
            rows = [{'秒': elapsed, 'a': False, 'b': False, 'Manager会话可用': True, 'Manager响应秒': 0.1}
                    for elapsed in (0, 3, 8)]
            (root / 'availability' / 'legacy.json').write_text(json.dumps({'操作': '旧匿名探针', '样本': rows}))
            result = collect(root)['可用性'][0]
            self.assertIsNone(result['a估算不可用秒'])
            self.assertEqual(result['a未确认样本数'], 3)

    def test_real_archive_truncation_preserves_prefix_and_removes_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / 'source.zip', Path(folder) / 'truncated.zip'
            with zipfile.ZipFile(source, 'w') as archive:
                archive.writestr('actual-body', b'body' * 4096)
            rewrite_archive(source, target, None, 'truncate')
            self.assertEqual(target.stat().st_size, source.stat().st_size - 4096)
            self.assertEqual(target.read_bytes(), source.read_bytes()[:-4096])
            self.assertFalse(zipfile.is_zipfile(target))

    def test_delayed_polling_does_not_shorten_actual_task_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / 'tasks').mkdir()
            (root / 'tasks' / 'backup.json').write_text(json.dumps({'status': 'failed', '总耗时秒': 3,
                'started_at': '2026-10-22T11:00:00', 'completed_at': '2026-10-22T11:10:00Z'}))
            task = collect(root)['任务'][0]
            self.assertEqual(task['总耗时秒'], 600)
            self.assertEqual(task['轮询观察秒'], 3)
        self.assertIsNone(elapsed_seconds('2026-10-22T11:10:00', '2026-10-22T11:00:00'))
        self.assertIsNone(elapsed_seconds('2026-10-22T11:00:00', None))

    def test_attack_archive_keeps_valid_manifest_and_snapshot_control(self):
        body = synthetic_archive([('../escape', b'path-proof', 0o100600)])
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            manifest = json.loads(archive.read('manager-manifest.json'))
            self.assertEqual(manifest['文件']['../escape']['摘要'], hashlib.sha256(b'path-proof').hexdigest())
            self.assertEqual(json.loads(archive.read('manager-snapshot.json'))['版本'], 1)
            self.assertEqual(set(manifest['文件']), set(archive.namelist()) - {'manager-manifest.json'})

    def test_suspension_and_missing_heartbeats_cannot_pass_soak(self):
        previous = {'单调秒': 10, '墙钟秒': 1000}
        self.assertIsNone(continuity_issue(previous, {'单调秒': 45, '墙钟秒': 1035}))
        self.assertIsNotNone(continuity_issue(previous, {'单调秒': 45, '墙钟秒': 4635}))
        self.assertIsNotNone(continuity_issue(previous, {'单调秒': 140, '墙钟秒': 1130}))
        self.assertIsNotNone(continuity_issue(previous, {'单调秒': 45, '墙钟秒': 900}))

    def test_phase_clock_gap_is_exposed_instead_of_counted_as_performance(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'phase-runs').mkdir()
            (root / 'phase-runs' / 'phase.json').write_text(json.dumps({'阶段': 'oracle', '状态': '执行完成',
                '开始时间UTC': '2026-10-22T11:00:00+00:00', '结束时间UTC': '2026-10-22T12:02:00+00:00', '耗时秒': 120}))
            phase = collect(root)['阶段耗时'][0]
            self.assertEqual(phase['时钟差额秒'], 3600)
            self.assertFalse(phase['连续性能测量可用'])

    def test_declared_volume_mutation_does_not_expand_real_content(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / 'source.zip', Path(folder) / 'target.zip'
            with zipfile.ZipFile(source, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('repos/sample/HEAD', '真实小正文')
                archive.writestr('manager-manifest.json', '{}')
            rewrite_archive(source, target, {'repos/sample/HEAD'}, 'volume')
            with zipfile.ZipFile(target) as archive:
                self.assertEqual(archive.getinfo('repos/sample/HEAD').file_size, 0xF0000000)
                with archive.open('repos/sample/HEAD') as stream:
                    self.assertEqual(stream.read(1024), '真实小正文'.encode())
                self.assertEqual(archive.getinfo('manager-manifest.json').file_size, 2)
            self.assertLess(target.stat().st_size, 1024)

    def test_manager_storage_can_only_mask_fixed_bounded_directories(self):
        for mode in ('backup-full', 'backup-inodes', 'backup-readonly', 'temporary-full'):
            mount = manager_storage_mount(mode)
            self.assertIn('size=16m', mount)
            self.assertIn(mount.split(':', 1)[0], ('/app/data/backups', '/tmp'))
        self.assertIn('nr_inodes=128', manager_storage_mount('backup-inodes'))
        self.assertIn(':ro,', manager_storage_mount('backup-readonly'))
        with self.assertRaises(RuntimeError):
            manager_storage_mount('/app/data')

    def test_long_run_budget_retains_all_twelve_real_archives(self):
        gib = 1024**3
        self.assertEqual(archive_budget([2 * gib], future_backups=12), 38 * gib)
        self.assertEqual(archive_budget([2 * gib]), 14 * gib)
        self.assertEqual(archive_budget([]), 10 * gib)
        with self.assertRaises(ValueError):
            archive_budget([gib], future_backups=-1)

    def audit_fixture(self):
        original = {'governance_audit_event': [{'id': 1, 'event_id': 'original', 'type': 'repository.created'}],
                    'governance_audit_scope': [{'id': 1, 'event_sequence': 1, 'scope_type': 'instance', 'scope_id': 0}],
                    'governance_repository_creation': [{'id': 'pending', 'state': 'unknown', 'origin': 'import',
                        'repo_id': 8, 'last_reason_code': 'completion_marker_missing', 'object_path': 'lab/proof',
                        'ancestor_i_ds': '[]'}]}
        actual = copy.deepcopy(original)
        actual['governance_audit_event'].append({'id': 2, 'event_id': 'new',
            'type': 'repository.creation_recovery_required', 'result': 'unknown', 'actor_id': 0,
            'actor': json.dumps({'kind': 'system', 'transport': 'background', 'name': '项目创建恢复核对任务'}),
            'details': json.dumps({'operation_id': 'pending', 'origin': 'import', 'reason_code': 'completion_marker_missing'}),
            'scope_type': 'repository', 'scope_id': 8, 'object_type': 'repository', 'object_id': 8,
            'object_path': 'lab/proof', 'ancestor_i_ds': '[]', 'occurred_at': '2026-10-22T10:00:00'})
        actual['governance_audit_scope'] += [{'id': identifier, 'event_sequence': 2, 'scope_type': kind,
            'scope_id': scope, 'occurred_at': '2026-10-22T10:00:00'}
            for identifier, kind, scope in ((2, 'instance', 0), (3, 'repository', 8))]
        return original, actual

    def test_only_known_native_recovery_audit_can_append(self):
        original, actual = self.audit_fixture()
        self.assertTrue(recovery_append_only(original, actual))

    def test_missing_original_audit_or_scope_is_detected(self):
        for table in ('governance_audit_event', 'governance_audit_scope'):
            original, actual = self.audit_fixture()
            actual[table].pop(0)
            self.assertFalse(recovery_append_only(original, actual))

    def test_changed_original_audit_is_detected(self):
        original, actual = self.audit_fixture()
        actual['governance_audit_event'][0]['type'] = 'user.security_changed'
        self.assertFalse(recovery_append_only(original, actual))

    def test_unexpected_audit_event_is_not_silently_allowed(self):
        original, actual = self.audit_fixture()
        actual['governance_audit_event'][1]['type'] = 'user.security_changed'
        self.assertFalse(recovery_append_only(original, actual))

    def test_recovery_audit_requires_matching_reason_and_complete_scopes(self):
        original, actual = self.audit_fixture()
        actual['governance_audit_scope'].pop()
        self.assertFalse(recovery_append_only(original, actual))
        original, actual = self.audit_fixture()
        actual['governance_audit_event'][1]['details'] = json.dumps({
            'operation_id': 'pending', 'origin': 'import', 'reason_code': 'other'})
        self.assertFalse(recovery_append_only(original, actual))

    def test_resource_units_do_not_inflate_or_silently_zero_samples(self):
        self.assertEqual(byte_count('1.5GiB'), 1610612736)
        self.assertEqual(byte_count('1.5GB'), 1500000000)
        self.assertEqual(byte_count('35.65MiB'), int(35.65 * 1024**2))
        with self.assertRaises(ValueError):
            byte_count('不可用')

    def test_disruptions_are_bounded_to_registered_node_and_service(self):
        self.assertEqual(disruption_rules('10.231.20.10', 'from-node'),
                         [['FORWARD', '-s', '10.231.20.10', '-j', 'DROP']])
        self.assertEqual(len(disruption_rules('10.231.20.10', 'both')), 2)
        self.assertIn('tcp-reset', disruption_rules('10.231.20.10', 'reset')[0])
        self.assertIn('3000', disruption_rules('10.231.20.10', 'api-only')[0])
        self.assertIn('22', disruption_rules('10.231.20.10', 'ssh-only')[0])
        with self.assertRaises(RuntimeError):
            disruption_rules('10.231.20.10', 'arbitrary-command')
        with self.assertRaises(ValueError):
            disruption_rules('10.231.20.10; dangerous', 'both')

    def test_unexecuted_mandatory_cases_never_pass(self):
        result = verdict([{'类别': 'topology', '状态': '通过'}])
        self.assertEqual(result['功能验收'], 'INCOMPLETE')
        self.assertEqual(result['类别']['actions'], '未执行')
        self.assertEqual(verdict([{'类别': 'soak-preflight', '状态': '通过'}])['类别']['soak'], '未执行')

    def test_first_failure_cannot_be_erased_by_rerun(self):
        records = [{'类别': key, '状态': '通过'} for key in CASES]
        records[:0] = [{'类别': 'normal', '状态': '失败'}]
        self.assertEqual(verdict(records)['功能验收'], 'FAIL')

    def test_environment_block_can_resolve_after_a_real_complete_pass(self):
        records = [{'类别': 'soak', '状态': '环境阻塞'}, {'类别': 'soak', '状态': '通过'}]
        self.assertEqual(verdict(records)['类别']['soak'], '通过')
        self.assertEqual(verdict(records)['功能验收'], 'INCOMPLETE')
        self.assertEqual(records[0]['状态'], '环境阻塞')

    def test_all_required_categories_needed(self):
        records = [{'类别': key, '状态': '通过'} for key in CASES]
        self.assertEqual(verdict(records)['功能验收'], 'PASS')
        records[-1]['状态'] = '环境阻塞'
        self.assertEqual(verdict(records)['功能验收'], 'INCOMPLETE')

    def test_detail_failure_is_not_hidden_by_grouping(self):
        self.assertEqual(verdict([{'类别': 'transfer', '状态': '失败'}])['功能验收'], 'FAIL')

    def test_fixture_error_is_retained_without_inventing_product_failure(self):
        records = [{'类别': 'seed', '状态': '失败', '说明': '夹具使用了错误的认证协议'}]
        result = verdict(records)
        self.assertEqual(result['功能验收'], 'INCOMPLETE')
        self.assertEqual(result['准备阶段首败数'], 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(verdict([{'类别': 'integrations-seed', '状态': '失败'}])['功能验收'], 'INCOMPLETE')
        checkpoint = verdict([{'类别': 'checkpoint', '状态': '失败'}])
        self.assertEqual(checkpoint['准备阶段首败数'], 1)
        self.assertEqual(checkpoint['功能检查失败数'], 0)

    def test_subnets_avoid_entire_existing_ranges(self):
        chosen = select_networks(['10.231.0.0/16', '192.168.0.0/16', 'fd00::/64'])
        for network in chosen:
            self.assertFalse(ipaddress.ip_network(network).overlaps(ipaddress.ip_network('10.231.0.0/16')))
        self.assertEqual(len(set(chosen)), 3)

    def test_no_space_is_explicit_failure(self):
        with self.assertRaises(RuntimeError):
            select_networks(['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'])

    def test_abbreviated_vpn_route_is_not_ignored(self):
        self.assertEqual(route_network('10/8'), '10.0.0.0/8')
        self.assertEqual(route_network('172.16/12'), '172.16.0.0/12')
        self.assertIsNone(route_network('default'))

    def test_foreign_container_cannot_be_operated(self):
        with patch('remote_backup.host.output', return_value=json.dumps([{'Config': {'Labels': {LABEL: 'other-run'}}}])):
            with self.assertRaises(RuntimeError):
                assert_owned(Path('/tmp/our-run'), 'foreign')

    def test_sensitive_state_has_restrictive_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'private.json'
            write_json(path, {'说明': '测试凭据'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text()), {'说明': '测试凭据'})

    def test_multipart_length_and_binary_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'backup.zip'
            body = bytes(range(256)) * 8193
            path.write_bytes(body)
            payload = MultipartArchive(path, 'test-boundary')
            data = b''.join(payload)
            self.assertEqual(len(data), len(payload))
            self.assertEqual(data[len(payload.prefix):-len(payload.suffix)], body)

    def test_large_upload_uses_bounded_chunks(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'large.zip'
            with path.open('wb') as stream:
                stream.truncate(2 * 1024**3)
            payload = MultipartArchive(path, 'test-boundary')
            chunks = iter(payload)
            next(chunks)
            self.assertEqual(len(next(chunks)), 1024 * 1024)
            self.assertGreater(len(payload), 2 * 1024**3)
            chunks.close()

    def test_checkpoint_name_cannot_escape_fixed_directory(self):
        for value in ('../outside', '/lab', 'bad;command', 'a/b', ''):
            with self.subTest(value=value), self.assertRaises(CheckFailure):
                checkpoint_name(value)
        self.assertEqual(checkpoint_name('fault-base'), 'fault-base')

    def test_driver_evidence_keeps_first_source_for_same_image(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'context' / 'remote_backup'
            source.mkdir(parents=True)
            (source / 'driver.py').write_text('说明 = "原始驱动"\n')
            freeze_driver_source(root, source.parent, 'sha256:fixed')
            (source / 'driver.py').write_text('说明 = "后来修改"\n')
            freeze_driver_source(root, source.parent, 'sha256:fixed')
            self.assertIn('原始驱动', (root / 'test-versions' / 'fixed' / 'driver.py').read_text())

    def test_availability_uses_elapsed_time_instead_of_sample_count(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'availability').mkdir()
            rows = [{'秒': elapsed, 'a': status, 'b': True, 'Manager会话可用': True, 'Manager响应秒': 0.1}
                    for elapsed, status in ((0, True), (3, False), (8, None), (10, True))]
            (root / 'availability' / 'run.json').write_text(json.dumps({'操作': '真实采样', '探针版本': 2, '样本': rows}))
            result = collect(root)['可用性'][0]
            self.assertEqual(result['a估算不可用秒'], 5)
            self.assertEqual(result['最大采样间隔秒'], 5)
            self.assertEqual(result['a未确认样本数'], 1)


if __name__ == '__main__':
    unittest.main()
