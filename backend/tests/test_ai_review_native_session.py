"""原生审查会话、事件、安全工作区与结果门禁回归。"""
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

from cryptography.fernet import Fernet

from ai_review.common import ReviewError, encode
from ai_review.diagnostics import safe_remote
from ai_review.native_review import _capability, _observed_reads, acknowledge_result, adapt, execute
from ai_review.native_workspace import bind_snapshot_evidence, cached_archive, safe_extract
from ai_review.session_events import create_session, iter_export, mark_submitted, project, query, receive
from ai_review.store import Store
from ai_review.worker import (_cleanup_owned, _failure_diagnostic, _finish_failure,
                              _persisted_native_result, _read_failure_actual)


class NativeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        key = self.root / '主密钥'; key.write_bytes(Fernet.generate_key())
        env = patch.dict(os.environ, {'AI_REVIEW_MASTER_KEY_FILE': str(key)})
        env.start(); self.addCleanup(env.stop)
        self.store = Store(self.root / 'review.db')
        self.store.save_config({'revision': 1, 'enabled': True, 'debounce_seconds': 0,
                                'model': '测试模型', 'engine': 'opencode_native'})
        self.repo = self.store.save_repo(1, 7, {'enabled': True,
            'bot_token': self.store.seal('仅测试凭据'), 'webhook_secret': self.store.seal('签名秘密'),
            'repo_name': 'test/code', 'bot_id': 9})['id']
        self.ident = self.store.enqueue(self.repo, 1, {'head': 'a'*40, 'base': 'b'*40})
        self.job = self.store.claim()
        # 本夹具模拟升级前的单会话任务。
        with self.store.tx() as c: c.execute("UPDATE jobs SET batch_protocol=0 WHERE id=?", (self.ident,))
        self.job["batch_protocol"] = 0

    def event(self, session, seq, kind, status='running', payload=None):
        return {'job_id': self.ident, 'session_id': session, 'seq': seq,
                'event_id': f'{session}:{seq}', 'type': kind, 'status': status,
                'target': '', 'summary': kind, 'created_at': time.time(), 'payload': payload or {}}

    def test_suboperation_completion_does_not_finish_session_and_batch_is_idempotent(self):
        session, token, _ = create_session(self.store, self.ident, self.job['owner'], time.time()+600,
                                           '/app/review-workspaces/'+self.ident)
        token = mark_submitted(self.store, self.ident, self.job['owner'], token)
        first = self.event(session, 1, 'model.completed', 'completed')
        second = self.event(session, 2, 'tool.running', 'running', {'call_id':'tool-1'})
        ack = receive(self.store, token, [first, second], return_ack=True)
        self.assertEqual(ack, {'inserted':2, 'session_id':session, 'acked_seq':2})
        row = project(self.store.job(self.ident, evidence=False))
        self.assertEqual(row['session']['status'], 'submitted')
        self.assertFalse(row['log_sync']['complete'])
        ack = receive(self.store, token, [first, second], return_ack=True)
        self.assertEqual(ack, {'inserted':0, 'session_id':session, 'acked_seq':2})
        tool_done = self.event(session, 3, 'tool.completed', 'completed', {'call_id':'tool-1'})
        heartbeat = self.event(session, 4, 'heartbeat', 'running')
        compaction = self.event(session, 5, 'session.compaction', 'completed')
        receive(self.store, token, [tool_done, heartbeat, compaction])
        row = project(self.store.job(self.ident, evidence=False))
        self.assertEqual(row['session']['status'], 'submitted')
        self.assertEqual(row['counters']['tool_calls'], 1)
        self.assertFalse(row['log_sync']['complete'])
        final = self.event(session, 6, 'session.completed', 'completed', {'result': {'findings': []}})
        self.assertEqual(receive(self.store, token, final), 1)
        self.assertFalse(project(self.store.job(self.ident,evidence=False))['log_sync']['complete'])
        self.store.update(self.ident, self.job['owner'], final_event_seq=6)
        self.assertFalse(project(self.store.job(self.ident,evidence=False))['log_sync']['complete'])
        self.store.update(self.ident, self.job['owner'], event_acked_seq=6, log_stream_closed=1)
        row = project(self.store.job(self.ident, evidence=False))
        self.assertEqual(row['session']['status'], 'completed')
        self.assertTrue(row['log_sync']['complete'])

    def test_event_sequence_conflict_and_redaction(self):
        session, token, _ = create_session(self.store, self.ident, self.job['owner'], time.time()+600, '/app/review-workspaces/'+self.ident)
        token = mark_submitted(self.store, self.ident, self.job['owner'], token)
        event = self.event(session, 1, 'session.running', payload={'Authorization':'Bearer secret-value', 'text':'token abcdefghijk'})
        receive(self.store, token, event)
        item = query(self.store, self.ident)['items'][0]
        self.assertEqual(item['payload']['Authorization'], '[已脱敏]')
        self.assertNotIn('abcdefghijk', encode(item))
        changed = dict(event, event_id=session+':冲突')
        with self.assertRaises(ReviewError): receive(self.store, token, changed)

    def test_budget_edit_does_not_invalidate_frozen_task(self):
        old = self.store.job_config(self.ident)['review_timeout_seconds']
        config = self.store.config()
        self.store.save_config({'revision': config['revision'], 'review_timeout_seconds': old+1})
        self.store.active(self.ident, self.job['owner'])
        self.assertEqual(self.store.job_config(self.ident)['review_timeout_seconds'], old)

    def test_reconcile_same_version_does_not_supersede_active_manual_review(self):
        signature = {'head':'c'*40, 'base':'d'*40}
        manual = self.store.enqueue(self.repo, 2, signature, manual=True)
        self.assertIsNone(self.store.enqueue(self.repo, 2, signature))
        self.assertEqual(self.store.job(manual, evidence=False)['status'], 'queued')

    def test_native_text_snapshot_binds_rename_add_delete_and_crlf(self):
        workspace = self.root / 'text-snapshot'
        for side in ('before', 'after'):
            (workspace/side).mkdir(parents=True)
        (workspace/'before/旧名.py').write_bytes(b'print("old")\r\n')
        (workspace/'after/新名.py').write_bytes(b'print("new")\r\n')
        (workspace/'after/新增.txt').write_bytes(b'added\n')
        (workspace/'before/删除.txt').write_bytes(b'deleted\n')
        diff = (
            'diff --git "a/\\346\\227\\247\\345\\220\\215.py" "b/\\346\\226\\260\\345\\220\\215.py"\n'
            'similarity index 50%\nrename from 旧名.py\nrename to 新名.py\n'
            '--- "a/\\346\\227\\247\\345\\220\\215.py"\n+++ "b/\\346\\226\\260\\345\\220\\215.py"\n'
            '@@ -1 +1 @@\n-print("old")\r\n+print("new")\r\n'
            'diff --git "a/\\346\\226\\260\\345\\242\\236.txt" "b/\\346\\226\\260\\345\\242\\236.txt"\n'
            'new file mode 100644\n--- /dev/null\n+++ "b/\\346\\226\\260\\345\\242\\236.txt"\n'
            '@@ -0,0 +1 @@\n+added\n'
            'diff --git "a/\\345\\210\\240\\351\\231\\244.txt" "b/\\345\\210\\240\\351\\231\\244.txt"\n'
            'deleted file mode 100644\n--- "a/\\345\\210\\240\\351\\231\\244.txt"\n+++ /dev/null\n'
            '@@ -1 +0,0 @@\n-deleted\n')
        (workspace/'变更.diff').write_bytes(diff.encode())
        snapshot = {'id':'fixed-snapshot','head':'a'*40,'base':'b'*40,
                    'merge_base':'b'*40,'files':[],'anchors':{}}
        result = bind_snapshot_evidence(self.store, self.job, snapshot, str(workspace), time.time()+30)
        self.assertEqual([row['path'] for row in result['files']], ['新名.py','新增.txt','删除.txt'])
        anchors = list(result['anchors'].values())
        self.assertTrue(any(row['path']=='新名.py' and row['side']=='new' and
                            row['line']==1 and row['quote']=='print("new")' for row in anchors))
        renamed = result['files'][0]
        self.assertEqual(renamed['old'], 'print("old")\n'.replace('\n','\r\n'))
        self.assertEqual(renamed['new'], 'print("new")\n'.replace('\n','\r\n'))
        self.assertTrue(result['coverage']['complete'])

    def test_native_snapshot_source_diff_mismatch_is_fatal(self):
        workspace = self.root / 'mismatch'
        (workspace/'before').mkdir(parents=True); (workspace/'after').mkdir()
        (workspace/'before/code.py').write_text('old\n')
        (workspace/'after/code.py').write_text('not-the-diff-result\n')
        (workspace/'变更.diff').write_text(
            'diff --git a/code.py b/code.py\n--- a/code.py\n+++ b/code.py\n'
            '@@ -1 +1 @@\n-old\n+new\n')
        snapshot = {'id':'fixed','head':'a'*40,'base':'b'*40,'merge_base':'b'*40,
                    'files':[],'anchors':{}}
        with self.assertRaises(ReviewError) as raised:
            bind_snapshot_evidence(self.store, self.job, snapshot, str(workspace), time.time()+30)
        self.assertTrue(getattr(raised.exception, 'fatal_job', False))

    def test_engine_migration_preserves_execution_identity_and_stops_old_work(self):
        for index, engine in enumerate(('opencode_cli', 'new_api_http')):
            database = self.root / f'legacy-{index}.db'
            legacy = Store(database)
            legacy.save_config({'revision':1, 'enabled':True, 'debounce_seconds':0,
                                'model':'旧模型', 'engine':'opencode_native'})
            repository = legacy.save_repo(1, index+20, {'enabled':True,
                'bot_token':legacy.seal('旧凭据'), 'webhook_secret':legacy.seal('旧签名'),
                'repo_name':f'test/legacy-{index}', 'bot_id':9})['id']
            ident = legacy.enqueue(repository, 1, {'head':'c'*40, 'base':'d'*40})
            with legacy.tx() as c:
                config = legacy.config(connection=c)
                config.update(engine=engine, revision=50, execution_revision=2)
                c.execute('UPDATE settings SET value=? WHERE id=1', (encode(config),))
                public = dict(config); public['api_key_configured'] = bool(public.pop('api_key', ''))
                c.execute('INSERT OR REPLACE INTO configuration_history VALUES (?,?,?)',
                          (50, encode(public), time.time()))
                c.execute("UPDATE jobs SET config_revision=50,status='queued' WHERE id=?", (ident,))
                signature = c.execute('SELECT signature FROM pr_state WHERE repository=? AND number=1',
                                      (repository,)).fetchone()[0]
                c.execute('PRAGMA user_version=5')
            migrated = Store(database)
            current = migrated.config()
            self.assertEqual(current['engine'], 'opencode_native')
            self.assertEqual(current['execution_revision'], 2)
            self.assertEqual(migrated.job(ident, evidence=False)['status'], 'superseded')
            with migrated.connect() as c:
                old = json.loads(c.execute('SELECT value FROM configuration_history WHERE revision=50').fetchone()[0])
                self.assertEqual(old['engine'], engine)
                self.assertEqual(c.execute('SELECT signature FROM pr_state WHERE repository=? AND number=1',
                                           (repository,)).fetchone()[0], signature)
            migrated.save_config({'revision':current['revision'],
                                  'review_timeout_seconds':current['review_timeout_seconds']+1})
            self.assertEqual(migrated.config()['execution_revision'], 2)

    def test_lease_takeover_reauthorizes_same_session_without_prompt_replay(self):
        session_id, event_token, deadline = create_session(
            self.store, self.ident, self.job['owner'], time.time()+600,
            '/app/review-workspaces/'+self.ident)
        mark_submitted(self.store, self.ident, self.job['owner'], event_token)
        old_token = _capability(self.store, self.job, '测试模型', deadline-40, -1)
        old_hash = hashlib.sha256(old_token.encode()).hexdigest()
        with self.store.tx() as c:
            c.execute('INSERT INTO review_native_bindings VALUES (?,?,?,?,?,1)',
                      (old_hash, self.ident, session_id, '测试模型', deadline-40))
            c.execute('UPDATE jobs SET lease=? WHERE id=?', (time.time()-1, self.ident))
        replacement = self.store.claim()
        self.assertNotEqual(replacement['owner'], self.job['owner'])
        with patch('ai_review.native_review.abort') as abort, patch('ai_review.native_workspace.cleanup') as cleanup:
            self.assertFalse(_cleanup_owned(self.store, self.job))
            abort.assert_not_called(); cleanup.assert_not_called()

        class Response:
            status_code = 200
            def __init__(self, value): self.value=value; self.content=encode(value).encode()
            def json(self): return self.value
        calls = []
        def request(_session, method, path, **kwargs):
            calls.append((method, path, kwargs.get('json')))
            return Response({'session_id':session_id, 'status':'running'} if path.endswith('/authorize')
                            else {'session_id':session_id, 'status':'completed', 'final_event_seq':1,
                                  'acked_seq':1,'log_stream_closed':True,'result':{'findings':[]}})
        config = self.store.job_config(self.ident)
        terminal=self.event(session_id,1,'session.completed','completed',
            {'result_sha256':hashlib.sha256(encode({'findings':[]}).encode()).hexdigest(),
             'finding_count':0,'check_count':0,'review_complete':True})
        receive(self.store,event_token,terminal)
        with patch('ai_review.native_review._request', side_effect=request):
            result=execute(self.store, replacement, config,
                           '/app/review-workspaces/'+self.ident, None)
        self.assertEqual(result['findings'],[])
        self.assertTrue(result['_event_log_complete'])
        self.assertEqual(result['_native_final_event_seq'],1)
        self.assertEqual([path for _, path, _ in calls],
                         [f'/sessions/{session_id}/authorize', f'/sessions/{session_id}'])
        self.assertFalse(any(path == '/execute' for _, path, _ in calls))
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT active FROM review_native_bindings WHERE token_hash=?',
                                       (old_hash,)).fetchone()[0], 0)
            caps = c.execute('SELECT owner,remaining FROM capabilities WHERE job=?', (self.ident,)).fetchall()
        self.assertEqual([(row['owner'], row['remaining']) for row in caps], [(replacement['owner'], -1)])

    def test_terminal_session_rejects_reauthorize_then_gets_without_replay(self):
        session_id, event_token, deadline = create_session(
            self.store, self.ident, self.job['owner'], time.time()+600,
            '/app/review-workspaces/'+self.ident)
        mark_submitted(self.store, self.ident, self.job['owner'], event_token)
        terminal=self.event(session_id,1,'session.completed','completed',
            {'result_sha256':hashlib.sha256(encode({'findings':[]}).encode()).hexdigest(),
             'finding_count':0,'check_count':0,'review_complete':True})
        receive(self.store,event_token,terminal)

        class Response:
            def __init__(self, status_code, value):
                self.status_code=status_code;self.value=value;self.content=encode(value).encode()
            def json(self):return self.value
        calls=[]
        def request(_session, method, path, **kwargs):
            calls.append((method,path))
            if path.endswith('/authorize'):
                return Response(409, {'status':'completed'})
            return Response(200, {'session_id':session_id,'status':'completed','final_event_seq':1,
                'acked_seq':1,'log_stream_closed':True,'result':{'findings':[]}})
        with patch('ai_review.native_review._request', side_effect=request):
            result=execute(self.store,self.job,self.store.job_config(self.ident),
                           '/app/review-workspaces/'+self.ident,None)
        self.assertTrue(result['_event_log_complete'])
        self.assertEqual(calls,[('POST',f'/sessions/{session_id}/authorize'),
                                ('GET',f'/sessions/{session_id}')])
        self.assertFalse(any(path == '/execute' for _,path in calls))
        row=project(self.store.job(self.ident,evidence=False))
        self.assertTrue(row['log_sync']['complete'])
        self.assertEqual(row['log_sync']['acked_seq'],1)
        self.assertTrue(row['log_sync']['stream_closed'])

    def test_persisted_result_is_acknowledged_idempotently_and_can_resume_without_get(self):
        session_id,_,_=create_session(self.store,self.ident,self.job['owner'],time.time()+600,
            '/app/review-workspaces/'+self.ident)
        sha='a'*64
        self.store.update(self.ident,self.job['owner'],snapshot={'id':'s'},result={'findings':[]},
                          native_result_sha256=sha,final_event_seq=3)
        resumed=self.store.job(self.ident,evidence=False)
        self.assertTrue(_persisted_native_result(resumed))
        class Response:
            status_code=200;content=b'{}'
            def json(self):return {'session_id':session_id,'status':'completed','result_released':True}
        with patch('ai_review.native_review._request',return_value=Response()) as request:
            acknowledge_result(self.store,self.job,sha,3)
            acknowledge_result(self.store,self.job,sha,3)
        self.assertEqual(request.call_count,2)
        self.assertTrue(project(self.store.job(self.ident,evidence=False))['session']['result_released'])

    def test_terminal_summary_must_match_authenticated_result(self):
        session_id,event_token,_=create_session(self.store,self.ident,self.job['owner'],time.time()+600,
            '/app/review-workspaces/'+self.ident)
        mark_submitted(self.store,self.ident,self.job['owner'],event_token)
        receive(self.store,event_token,self.event(session_id,1,'session.completed','completed',
            {'result_sha256':'0'*64,'finding_count':0,'check_count':0,'review_complete':True}))
        class Response:
            status_code=200
            content=encode({'status':'completed'}).encode()
            def json(self):return {'session_id':session_id,'status':'completed','final_event_seq':1,
                'acked_seq':1,'log_stream_closed':True,'result':{'findings':[]}}
        with patch('ai_review.native_review._request',return_value=Response()), \
             self.assertRaisesRegex(ReviewError,'结果与终态摘要不一致'):
            execute(self.store,self.job,self.store.job_config(self.ident),
                    '/app/review-workspaces/'+self.ident,None)

    def test_historical_job_does_not_get_empty_native_session(self):
        with self.store.tx() as c:
            c.execute("UPDATE jobs SET session_id='',session_status='' WHERE id=?", (self.ident,))
        row = project(self.store.job(self.ident, evidence=False))
        self.assertFalse(row['native_session'])
        self.assertNotIn('session', row)
        self.assertNotIn('log_sync', row)

    def test_model_http_error_remains_primary_when_native_session_then_exits(self):
        actual=self.store.job(self.ident,evidence=False)
        actual['stage']='原生 OpenCode 会话自主审查'
        actual['diagnostic']={'status':'failed','source':'model_service',
            'error_code':'MODEL_HTTP_ERROR','message':'模型服务返回 HTTP 503，未自动重放',
            'upstream_http_status':503,'call_id':'call-1'}
        error=ReviewError('原生审查未完成：cli_exit')
        error.error_code='EXECUTOR_CLI_EXIT'
        diagnostic,message=_failure_diagnostic(actual,error,self.job)
        self.assertEqual(diagnostic['error_code'],'MODEL_HTTP_ERROR')
        self.assertEqual(diagnostic['upstream_http_status'],503)
        self.assertEqual(diagnostic['executor_error_code'],'EXECUTOR_CLI_EXIT')
        self.assertEqual(diagnostic['executor_phase'],actual['stage'])
        self.assertEqual(message,actual['diagnostic']['message'])

    def test_model_upstream_first_cause_keeps_native_unknown_as_secondary(self):
        actual=self.store.job(self.ident,evidence=False)
        actual['stage']='原生 OpenCode 会话自主审查'
        actual['diagnostic']={'status':'failed','source':'model_upstream',
            'error_code':'MODEL_UPSTREAM_ERROR','message':'模型服务流返回错误，未自动重放',
            'reason':'upstream_sse_error','upstream_http_status':200,
            'upstream_error_type':'internal_error','call_id':'call-1','session_id':'session-1'}
        error=ReviewError('原生模型消息执行失败')
        error.error_code='EXECUTOR_NATIVE_MESSAGE_ERROR'
        error.diagnostic={'status':'failed','source':'native_result','phase':'message_completion',
            'reason':'native_message_error','error_code':'native_message_error',
            'native_error_type':'UnknownError','call_id':'call-1','session_id':'session-1',
            'message':'不可信源码正文 Key=秘密'}
        diagnostic,message=_failure_diagnostic(actual,error,self.job)
        self.assertEqual(diagnostic['error_code'],'MODEL_UPSTREAM_ERROR')
        self.assertEqual(diagnostic['reason'],'upstream_sse_error')
        self.assertEqual(diagnostic['secondary']['native_error_type'],'UnknownError')
        self.assertEqual(diagnostic['secondary']['reason'],'native_message_error')
        self.assertNotIn('秘密',encode(diagnostic)+message)
        self.store.update(self.ident,self.job['owner'],diagnostic=actual['diagnostic'])
        _finish_failure(self.store,self.store.job(self.ident,evidence=False),error,self.job,'failed')
        public=self.store.job(self.ident,evidence=False)['diagnostic']
        self.assertEqual(public['error_code'],'MODEL_UPSTREAM_ERROR')
        self.assertEqual(public['secondary']['native_error_type'],'UnknownError')
        self.assertNotIn('message',public['secondary'])

    def test_failed_session_get_propagates_safe_json_location_to_public_task(self):
        session_id, token, _ = create_session(self.store, self.ident, self.job['owner'],
            time.time()+600, '/app/review-workspaces/'+self.ident)
        mark_submitted(self.store, self.ident, self.job['owner'], token)
        self.job = self.store.job(self.ident, evidence=False)
        class Response:
            status_code = 200
            content = b'{}'
            def __init__(self, value): self.value=value
            def json(self): return self.value
        evidence=json.loads((Path(__file__).parents[2]/'evidence/runtime-diagnostics-smoke.json').read_text())
        runtime=evidence['真实原生协议冒烟']['诊断']
        runtime.update(session_id=session_id,message='Authorization: Bearer secret-value',source_text='私有源码')
        failed = {'status':'failed', 'error_code':'invalid_json', 'session_id':session_id,
                  'diagnostic':runtime,'acked_seq':0,'log_stream_closed':False,
                  'raw_diagnostics':{'status':'available','available':True,'truncated':False,
                    'bytes':2048,'original_bytes':2048,'retention_expires_at':time.time()+3600,
                    'sha256':'c'*64,'job_id':self.ident,'session_id':session_id}}
        responses=[Response({'status':'running'}),Response(failed)]
        with patch('ai_review.native_review._request',side_effect=responses), \
             self.assertRaises(ReviewError) as raised:
            execute(self.store,self.job,self.store.job_config(self.ident),
                    '/app/review-workspaces/'+self.ident,token)
        _finish_failure(self.store,self.store.job(self.ident,evidence=False),raised.exception,self.job,'failed')
        task=self.store.job(self.ident,public=True,evidence=False); public=task['diagnostic']
        self.assertEqual(task['raw_diagnostics']['status'],'available')
        self.assertEqual(task['raw_diagnostics']['bytes'],2048)
        self.assertEqual(public['error_code'],'invalid_json')
        self.assertEqual(public['session_id'],session_id)
        self.assertEqual(public['call_id'],runtime['call_id'])
        self.assertEqual(public['json_offset'],13)
        self.assertEqual(public['received_bytes'],13)
        self.assertEqual(public['extraction_path'],'message.parts[type=text].text')
        self.assertEqual(public['parser_message'],'缺少 JSON 值')
        self.assertEqual(task['error'],'模型输出在 JSON 结束前中断，审查未完成')
        self.assertNotIn('message',safe_remote(failed['diagnostic']))
        self.assertNotIn('source_text',public)
        self.assertNotIn('secret-value',encode(public))

    def test_malformed_diagnostic_never_masks_original_failure(self):
        malformed={key:[] for key in ('source','phase','reason','extraction_path','category',
                                      'json_offset','completion_complete','elapsed_seconds','frames')}
        malformed['stack_location']={'file':[], 'function':{}, 'line':'1'}
        self.assertEqual(safe_remote(malformed),{})
        self.assertEqual(safe_remote({'request_started_at':1789550000.25})['request_started_at'],
                         1789550000.25)

    def test_failure_context_read_error_still_logs_primary_safe_diagnostic(self):
        error=ReviewError('Authorization Bearer private')
        error.error_code='EXECUTOR_INVALID_JSON'
        error.diagnostic={'source':'native_result','phase':'result_decode','reason':'unexpected_eof',
                          'error_code':'invalid_json','call_id':'call-safe','message':'源码正文'}
        broken=Mock(); broken.job.side_effect=OSError('Key=private')
        with self.assertLogs(level='ERROR') as logs:
            self.assertIsNone(_read_failure_actual(broken,self.job,error))
        output='\n'.join(logs.output)
        self.assertIn('call-safe',output)
        self.assertIn('invalid_json',output)
        self.assertNotIn('private',output)

    def test_failure_persistence_error_keeps_primary_in_container_log(self):
        error=ReviewError('受控本地错误')
        error.error_code='EXECUTOR_INVALID_JSON'
        error.diagnostic={'source':'native_result','phase':'result_decode','reason':'unexpected_eof',
                          'error_code':'invalid_json','call_id':'call-before-db'}
        broken=Mock(); broken.update.side_effect=OSError('Authorization Bearer private')
        with self.assertLogs(level='ERROR') as logs:
            _finish_failure(broken,self.store.job(self.ident,evidence=False),error,self.job,'failed')
        output='\n'.join(logs.output)
        self.assertIn('call-before-db',output)
        self.assertIn('invalid_json',output)
        self.assertNotIn('private',output)

    def test_different_call_does_not_merge_stale_primary_diagnostic(self):
        actual=self.store.job(self.ident,evidence=False)
        actual['diagnostic']={'status':'failed','source':'model_service','error_code':'MODEL_HTTP_ERROR',
                             'session_id':'session-1','call_id':'call-old','upstream_http_status':403}
        error=ReviewError('不可信错误 Key=秘密')
        error.error_code='EXECUTOR_INVALID_JSON'
        error.diagnostic={'status':'failed','source':'result_parser','error_code':'invalid_json',
                          'session_id':'session-1','call_id':'call-new','json_offset':19,
                          'message':'Key=秘密 源码正文'}
        diagnostic,message=_failure_diagnostic(actual,error,self.job)
        self.assertEqual(diagnostic['error_code'],'invalid_json')
        self.assertEqual(diagnostic['call_id'],'call-new')
        self.assertNotIn('upstream_http_status',diagnostic)
        self.assertNotIn('秘密',encode(diagnostic)+message)

    def test_cross_job_token_out_of_order_and_incremental_queries(self):
        session, token, _ = create_session(self.store, self.ident, self.job['owner'], time.time()+600,
                                           '/app/review-workspaces/'+self.ident)
        token = mark_submitted(self.store, self.ident, self.job['owner'], token)
        with self.assertRaises(ReviewError):
            receive(self.store, token, self.event(session, 2, 'tool.running', payload={'call_id':'x'}))
        self.assertEqual(query(self.store, self.ident)['items'], [])
        wrong = self.event(session, 1, 'tool.running', payload={'call_id':'x'})
        wrong['job_id'] = '0'*32
        with self.assertRaises(ReviewError): receive(self.store, token, wrong)
        rows = [self.event(session, 1, 'tool.running', payload={'call_id':'x','token':'敏感值'}),
                self.event(session, 2, 'tool.completed', 'completed', {'call_id':'x'}),
                self.event(session, 3, 'model.running', 'running', {'name':'搜索模型'})]
        receive(self.store, token, rows)
        self.assertEqual([row['seq'] for row in query(self.store,self.ident,after=1,limit=10)['items']], [2,3])
        self.assertEqual([row['seq'] for row in query(self.store,self.ident,before=3,limit=10)['items']], [1,2])
        self.assertEqual([row['type'] for row in query(self.store,self.ident,kinds=['model.running'])['items']],
                         ['model.running'])
        self.assertEqual([row['seq'] for row in query(self.store,self.ident,text='搜索')['items']], [3])
        exported = b''.join(iter_export(self.store,self.ident)).decode()
        self.assertNotIn('敏感值', exported)
        self.assertEqual(len(exported.splitlines()), 3)

    def test_events_http_requires_login_and_honors_cursor_search_export(self):
        from flask import Flask
        from tests.access_helpers import init_test_auth as init_auth, login_platform
        from routes.ai_review_routes import ai_review_bp
        session_id, token, _ = create_session(self.store,self.ident,self.job['owner'],time.time()+600,
            '/app/review-workspaces/'+self.ident)
        token=mark_submitted(self.store,self.ident,self.job['owner'],token)
        receive(self.store,token,[self.event(session_id,1,'tool.running',payload={'call_id':'http-1'}),
            self.event(session_id,2,'tool.completed','completed',{'call_id':'http-1','name':'命中搜索'})])
        app=Flask(__name__);app.config.update(SECRET_KEY='测试会话',AI_REVIEW_DB=str(self.store.path))
        init_auth(app);app.register_blueprint(ai_review_bp,url_prefix='/api/ai-review')
        client=app.test_client();url=f'/api/ai-review/tasks/{self.ident}/events'
        self.assertEqual(client.get(url).status_code,401)
        login_platform(client)
        response=client.get(url+'?after=1&type=tool.completed&q=命中&limit=1')
        self.assertEqual(response.status_code,200);self.assertEqual([v['seq'] for v in response.json['items']],[2])
        self.assertEqual(client.get(url+'?after=1&before=3').status_code,400)
        export=client.get(url+'/export?q=命中')
        self.assertEqual(export.status_code,200);self.assertEqual(len(export.data.splitlines()),1)

    def test_thirty_thousand_tool_events_remain_linear_and_allow_other_writes(self):
        session, token, _ = create_session(self.store, self.ident, self.job['owner'], time.time()+600,
                                           '/app/review-workspaces/'+self.ident)
        token = mark_submitted(self.store, self.ident, self.job['owner'], token)
        failure=[]
        def ingest():
            try:
                for offset in range(0,30000,100):
                    batch=[]
                    for seq in range(offset+1,offset+101):
                        call=f'tool-{(seq-1)//2}'
                        batch.append(self.event(session,seq,'tool.running' if seq%2 else 'tool.completed',
                            'running' if seq%2 else 'completed',{'call_id':call,'part_id':call,
                            'line_start':1,'line_end':20,'path':'变更.diff'}))
                    receive(self.store,token,batch)
            except BaseException as exc:failure.append(exc)
        started=time.monotonic();thread=threading.Thread(target=ingest);thread.start();latencies=[]
        while thread.is_alive():
            begin=time.monotonic();self.store.health('30k-并发探针','可写');latencies.append(time.monotonic()-begin)
            self.store.job(self.ident,evidence=False);time.sleep(.01)
        thread.join();elapsed=time.monotonic()-started
        if failure:raise failure[0]
        row=project(self.store.job(self.ident,evidence=False))
        self.assertEqual(row['log_sync']['last_seq'],30000)
        self.assertEqual(row['counters']['tool_calls'],15000)
        self.assertLess(elapsed,20)
        self.assertLess(max(latencies or [0]),2)

    def test_native_finding_keeps_inline_validation_feedback_id_and_dataset_export(self):
        from ai_review import dataset, feedback
        from ai_review.dataset import finding_id
        from ai_review.publish import Publisher, finding_body
        workspace=self.root/'native-loop';(workspace/'before').mkdir(parents=True);(workspace/'after').mkdir()
        old='int f(void) { return 0; }\n';new='int f(void) { return 1; }\n'
        (workspace/'before/code.c').write_text(old);(workspace/'after/code.c').write_text(new)
        (workspace/'源码清单.json').write_text(json.dumps({'变更文件':['code.c'],
            '修改前源码':{'未决内容':[]},'修改后源码':{'未决内容':[]}},ensure_ascii=False))
        anchor={'id':'anchor','snapshot_id':'snapshot','path':'code.c','old_path':'code.c',
                'side':'new','line':1,'quote':new.rstrip(),'ambiguous':False}
        snapshot={'id':'snapshot','head':'a'*40,'base':'b'*40,'merge_base':'b'*40,
            'anchors':{'anchor':anchor},'files':[{'path':'code.c','old':old,'new':new}]}
        raw={'coverage':{'reviewed_paths':['code.c'],'skipped':[]},'findings':[{
            'severity':'P1','title':'返回值错误','trigger':'调用 f','explanation':'返回契约改变',
            'impact':'调用者误判','recommendation':'恢复契约','path':'code.c','side':'new','line':1,
            'quote':new.rstrip(),'scope':'f','verification':'已核对调用者','assessment':'confirmed',
            'support':[{'path':'code.c','side':'before','quote':old.rstrip()}]}]}
        result=adapt(raw,snapshot,str(workspace),20)
        finding=result['findings'][0]
        publisher=Publisher(self.store,Mock(prefix='/repos/test/code'),self.job,self.store.repo(self.repo),snapshot,result)
        publisher.validate_local();self.assertIsNotNone(finding['anchor'])
        self.assertIn('意见编号：`AR-',finding_body(finding,self.job))
        self.store.update(self.ident,self.job['owner'],snapshot=snapshot,result=result,review_id=77,
                          review_fragment='issuecomment-77')
        self.store.finish(self.ident,self.job['owner'],'succeeded')
        dataset.archive_pending(self.store)
        archived_job=self.store.job(self.ident);ident=finding_id(archived_job,finding)
        with self.store.connect() as c:
            self.assertIsNotNone(c.execute('SELECT 1 FROM review_finding_archive WHERE id=?',(ident,)).fetchone())
        class Client:
            prefix='/repos/test/code'
            def request(self,method,path,*args,**kwargs):
                if path=='/users/writer':return {'id':1,'login':'writer'}
                if path.endswith('/permission'):return {'permission':'write'}
                raise AssertionError(path)
            def repository(self):return {'html_url':'http://gitea/test/code'}
        comment={'id':88,'body':f'/ai-review 反馈 {ident} 有效',
                 'html_url':'http://gitea/test/code/pulls/1#issuecomment-88'}
        sample=feedback.apply_command(self.store,Client(),self.store.repo(self.repo),1,comment,
                                      {'id':1,'login':'writer'})
        self.assertEqual(sample['status'],'active')
        with zipfile.ZipFile(dataset.export(self.store,self.repo,True)) as archive:
            exported=archive.read('审查数据集.jsonl').decode()
        self.assertIn(ident,exported);self.assertIn('返回值错误',exported)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def archive(self, rows):
        path = self.root / ('archive-' + str(len(list(self.root.iterdir()))) + '.tar')
        with tarfile.open(path, 'w') as archive:
            for name, kind, content in rows:
                info = tarfile.TarInfo(name)
                if kind == 'file':
                    raw = content.encode(); info.size = len(raw); archive.addfile(info, io.BytesIO(raw))
                elif kind == 'link':
                    info.type = tarfile.SYMTYPE; info.linkname = content; archive.addfile(info)
        return path

    def test_links_and_lfs_are_unresolved_without_following(self):
        archive = self.archive([('repo/code.c','file','int x;\n'), ('repo/link.c','link','/etc/passwd'),
            ('repo/large.bin','file','version https://git-lfs.github.com/spec/v1\noid sha256:'+'a'*64+'\nsize 9\n')])
        result = safe_extract(archive, self.root/'out')
        self.assertTrue((self.root/'out/code.c').is_file())
        self.assertFalse((self.root/'out/link.c').exists())
        self.assertEqual({r['类型'] for r in result['未决内容']}, {'符号链接或硬链接','Git LFS 指针'})

    def test_path_traversal_rejects_entire_archive(self):
        archive = self.archive([('../escape','file','x')])
        with self.assertRaises(ReviewError): safe_extract(archive, self.root/'bad')
        self.assertFalse((self.root/'bad').exists())

    def test_archive_member_limit_is_checked_while_scanning(self):
        archive = self.archive([('repo/a','file','a'), ('repo/b','file','b'), ('repo/c','file','c')])
        with patch('ai_review.native_workspace.MAX_FILES', 2), self.assertRaisesRegex(ReviewError, '文件数超过'):
            safe_extract(archive, self.root/'too-many')
        self.assertFalse((self.root/'too-many').exists())

    def test_archive_deadline_is_checked_while_scanning(self):
        archive = self.archive([('repo/a','file','a'), ('repo/b','file','b')])
        checks = 0
        def deadline(_value):
            nonlocal checks
            checks += 1
            if checks == 2: raise ReviewError('原生审查总期限已到，停止准备工作区')
        with patch('ai_review.native_workspace._deadline', side_effect=deadline), self.assertRaisesRegex(ReviewError, '总期限'):
            safe_extract(archive, self.root/'expired', deadline_at=time.time()+60)
        self.assertEqual(checks, 2)
        self.assertFalse((self.root/'expired').exists())

    def test_oversized_declared_member_is_rejected_from_header(self):
        archive = self.root/'oversized-header.tar'
        info = tarfile.TarInfo('repo/huge.c'); info.size = 3
        archive.write_bytes(info.tobuf(format=tarfile.USTAR_FORMAT) + b'\0' * 1024)
        with patch('ai_review.native_workspace.MAX_FILE_BYTES', 2), \
             self.assertRaisesRegex(ReviewError, '单文件超过'):
            safe_extract(archive, self.root/'oversized')
        self.assertFalse((self.root/'oversized').exists())

    def test_fixed_sha_cache_binds_instance_repository_and_redownloads_corruption(self):
        class Client:
            url='https://gitea.example';repo_id=7;repo_name='team/code';prefix='/repos/team/code'
            def check_active(self):pass
        client=Client();downloads=[]
        def download(_client,path,destination,*_args):
            downloads.append(path);destination.write_bytes(('archive-'+path).encode());return destination.stat().st_size
        cache=self.root/'cache'
        with patch('ai_review.native_workspace.CACHE_ROOT',cache), \
             patch('ai_review.native_workspace._download',side_effect=download), \
             patch('ai_review.native_workspace._cache_limit',return_value=2*1024**3):
            with cached_archive(client,'a'*40) as first:self.assertTrue(first.is_file())
            with cached_archive(client,'a'*40) as second:self.assertEqual(first,second)
            self.assertEqual(len(downloads),1)
            first.chmod(0o600);first.write_bytes('损坏'.encode())
            with cached_archive(client,'a'*40):pass
            self.assertEqual(len(downloads),2)
            client.repo_id=8
            with cached_archive(client,'a'*40):pass
            self.assertEqual(len(downloads),3)


class NativeResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'before').mkdir(); (self.root/'after').mkdir()
        (self.root/'before/code.c').write_text('int f(void) { return 0; }\n')
        (self.root/'after/code.c').write_text('int f(void) { return 1; }\n')
        (self.root/'源码清单.json').write_text(json.dumps({'变更文件':['code.c'],
            '修改前源码':{'未决内容':[]},'修改后源码':{'未决内容':[]}},ensure_ascii=False))
        (self.root/'变更.diff').write_text('\n'.join(f'diff-{n}' for n in range(1, 7))+'\n')
        self.anchor = {'id':'锚点','snapshot_id':'快照','path':'code.c','old_path':'code.c','side':'new',
                       'line':1,'quote':'int f(void) { return 1; }','ambiguous':False}
        self.snapshot = {'id':'快照','anchors':{'锚点':self.anchor},'files':[{'path':'code.c','old':'int f(void) { return 0; }\n',
            'new':'int f(void) { return 1; }\n'}]}

    def valid(self):
        return {'severity':'P1','title':'错误','trigger':'调用 f','explanation':'返回值改变','impact':'计算错误',
            'recommendation':'恢复契约','path':'code.c','side':'new','line':1,'quote':'int f(void) { return 1; }',
            'scope':'f','verification':'已检查调用约束','assessment':'confirmed',
            'support':[{'path':'code.c','side':'before','quote':'int f(void) { return 0; }'}]}

    def test_invalid_item_does_not_discard_valid_item_and_unobserved_is_incomplete(self):
        raw = {'coverage':{'reviewed_paths':['code.c'],'skipped':[]},
               'findings':[self.valid(), {'severity':'P1','path':'../bad'}]}
        result = adapt(raw, self.snapshot, str(self.root), 20)
        self.assertEqual(len(result['findings']), 1)
        self.assertEqual(len(result['invalid_findings']), 1)
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(result['findings'][0]['quality_version'], 3)

    def test_max_findings_is_publication_page_size_not_result_truncation(self):
        raw = {'coverage':{'reviewed_paths':['code.c'],'skipped':[]},
               'findings':[self.valid() for _ in range(4)]}
        result = adapt(raw, self.snapshot, str(self.root), 2)
        self.assertEqual(len(result['findings']), 4)
        self.assertEqual(result['publication_limit'], 2)

    def test_truncated_output_keeps_valid_finding_but_never_claims_complete(self):
        raw={'_review_completion':{'complete':False,'finish_reason':'length',
                                   'error':'invalid_json','json_offset':321},
             'findings':[self.valid()]}
        result=adapt(raw,self.snapshot,str(self.root),20)
        self.assertEqual(len(result['findings']),1)
        self.assertFalse(result['coverage']['complete'])
        self.assertFalse(result['coverage']['output_complete'])
        self.assertIn('原生输出完整性',{row['path'] for row in result['coverage']['skipped']})
        self.assertEqual(result['review_completion'],{'complete':False,'finish_reason':'length',
                                                       'error':'invalid_json','json_offset':321})
        reason=next(row['reason'] for row in result['coverage']['skipped']
                    if row['path']=='原生输出完整性')
        self.assertIn('finish_reason=length',reason)
        self.assertIn('JSON 偏移 321',reason)

    def test_bad_coverage_items_keep_independently_valid_finding(self):
        cases = [
            {'findings':[self.valid()]},
            {'coverage':{'reviewed_paths':['code.c','../越界.c'],'skipped':[]},
             'findings':[self.valid()]},
            {'coverage':{'reviewed_paths':['code.c'],'skipped':[{'path':'../越界.c','reason':'未读'}]},
             'findings':[self.valid()]},
        ]
        for raw in cases:
            with self.subTest(coverage=raw.get('coverage')):
                result=adapt(raw,self.snapshot,str(self.root),20)
                self.assertEqual(len(result['findings']),1)
                self.assertFalse(result['coverage']['complete'])
                self.assertIn('原生覆盖声明校验',
                              {row['path'] for row in result['coverage']['skipped']})

    def test_snapshot_evidence_gap_blocks_completion_but_location_only_skip_does_not(self):
        raw = {'coverage':{'reviewed_paths':['code.c'],'skipped':[]},'findings':[]}
        with patch('ai_review.native_review._observed_reads', return_value=(set(), True)):
            gap = dict(self.snapshot, coverage={'skipped':[{
                'path':'code.c','reason':'固定源码超过资源上限','evidence_gap':True}]})
            result = adapt(raw, gap, str(self.root), 20, Mock(), 'job')
            self.assertFalse(result['coverage']['complete'])
            self.assertEqual(result['coverage']['files_available'], 0)
            self.assertIn('固定源码超过资源上限',
                          {row['reason'] for row in result['coverage']['skipped']})
            location_only = dict(self.snapshot, coverage={'skipped':[{
                'path':'code.c','reason':'纯重命名不做行内定位','evidence_gap':False}]})
            self.assertTrue(adapt(raw, location_only, str(self.root), 20, Mock(), 'job')
                            ['coverage']['complete'])

    def test_read_coverage_requires_gapless_diff_and_only_changed_source_ranges(self):
        store = Store(self.root/'events.db')
        snapshot = {'files':[{'path':'code.c','old_path':'code.c','new_path':'code.c',
            'patch':[{'old_start':100,'old_count':3,'new_start':200,'new_count':4}]}]}
        def save(rows):
            with store.tx() as c:
                c.execute('DELETE FROM review_session_events')
                for seq, (path, start, end) in enumerate(rows, 1):
                    payload = encode({'tool':'read','path':path,'line_start':start,'line_end':end,
                                      'offset':start,'limit':end-start+1,'truncated':False,'has_more':False})
                    c.execute('INSERT INTO review_session_events VALUES (?,?,?,?,?,?,?,?,?,?)',
                              ('job','session_123456789',seq,f'session_123456789:{seq}','tool.completed',
                               'completed',path,'read',time.time(),payload))
        sources = [('before/code.c',100,102),('after/code.c',200,203)]
        save([('变更.diff',1,2),('变更.diff',4,6),*sources])
        observed, diff_read = _observed_reads(store,'job',str(self.root),{'code.c'},snapshot)
        self.assertFalse(diff_read); self.assertEqual(observed, {'code.c'})
        save([('变更.diff',1,6),('before/code.c',100,102),
              ('after/code.c',200,200),('after/code.c',202,203)])
        observed, diff_read = _observed_reads(store,'job',str(self.root),{'code.c'},snapshot)
        self.assertTrue(diff_read); self.assertEqual(observed, set())
        save([('变更.diff',1,6),*sources])
        observed, diff_read = _observed_reads(store,'job',str(self.root),{'code.c'},snapshot)
        self.assertTrue(diff_read); self.assertEqual(observed, {'code.c'})
        # 完整 Diff 已含修改前后变更原文；模型明确审查该文件后，无需重复读整个大源码。
        save([('变更.diff',1,6)])
        raw={'coverage':{'reviewed_paths':['code.c'],'skipped':[]},'findings':[]}
        result=adapt(raw,self.snapshot,str(self.root),20,store,'job')
        self.assertTrue(result['coverage']['complete'])
        self.assertEqual(result['coverage']['files_source_observed'],0)
        raw['_event_log_complete']=False;raw['_event_log_received']=5;raw['_event_log_expected']=6
        result=adapt(raw,self.snapshot,str(self.root),20,store,'job')
        self.assertFalse(result['coverage']['complete'])
        self.assertFalse(result['coverage']['event_log_complete'])


if __name__ == '__main__':
    unittest.main()
