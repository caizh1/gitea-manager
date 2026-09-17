"""展示复用的身份隔离、失效、跨进程去重与容量上限；不使用展示结果做合并授权。"""
import json
import multiprocessing
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from flask import Flask
from models import db,ApprovalPolicy,ApprovalViewCache
from approval.display_cache import display_status,context_hash
from approval.rules import ApprovalError


def app_at(path):
    app=Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///'+str(path),SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    return app


def worker(path,event,counter,queue):
    with app_at(path).app_context():
        policy=db.session.get(ApprovalPolicy,1)
        event.wait(5)
        def calculate():
            with counter.get_lock():counter.value+=1
            time.sleep(.3)
            return {'satisfied':True,'valid_until':time.time()+15,'approved':[{'id':7}]}
        try:queue.put(display_status(policy,1,calculate))
        except Exception as exc:queue.put({'异常':type(exc).__name__,'原因':str(exc)})


class DisplayCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'展示测试.db'
        self.app=app_at(self.path);self.ctx=self.app.app_context();self.ctx.push();db.create_all()
        self.policy=ApprovalPolicy(id=1,server_id=1,repo_id=2,repo_name='隔离/仓库',owner_id=3,rule_json='{}')
        db.session.add(self.policy);db.session.commit()
        self.calls=0

    def tearDown(self):
        db.session.remove();self.ctx.pop();self.temp.cleanup()

    def compute(self):
        self.calls+=1
        return {'satisfied':True,'checked_at':time.time(),'valid_until':time.time()+15,'approved':[{'id':7}]}

    def test_shared_snapshot_never_keeps_callers_identity_or_mutation(self):
        first=display_status(self.policy,1,self.compute)
        first['viewer_id']=10;first['approved'].clear()
        second=display_status(self.policy,1,self.compute)
        self.assertNotIn('viewer_id',second);self.assertEqual(second['approved'],[{'id':7}]);self.assertEqual(self.calls,1)

    def test_native_permission_check_remains_before_cache_entry(self):
        # 权限检查由路由执行；缓存模块没有 Cookie 或管理令牌输入，也不能产生 viewer_id。
        with self.assertRaisesRegex(ApprovalError,'浏览器身份'):
            display_status(self.policy,1,lambda:{'viewer_id':999,'satisfied':True})
        self.assertNotIn('999',db.session.get(ApprovalViewCache,(1,1)).payload)

    def test_configuration_change_and_expiry_require_new_calculation(self):
        display_status(self.policy,1,self.compute)
        self.policy.revision+=1;db.session.commit()
        display_status(self.policy,1,self.compute);self.assertEqual(self.calls,2)
        row=db.session.get(ApprovalViewCache,(1,1));row.expires_at=time.time()-1;db.session.commit()
        display_status(self.policy,1,self.compute);self.assertEqual(self.calls,3)

    def test_reused_policy_id_in_another_instance_never_reuses_old_scope(self):
        # SQLite 在删除最大策略 ID 后可重新使用该 ID；另一实例可能拥有相同仓库/PR编号和名称。
        first_context=context_hash(self.policy)
        display_status(self.policy,1,self.compute)
        self.policy.server_id=2;self.policy.owner_id=4;db.session.commit()
        self.assertNotEqual(context_hash(self.policy),first_context)
        display_status(self.policy,1,self.compute)
        self.assertEqual(self.calls,2)

    def test_failure_cannot_reuse_previous_success(self):
        display_status(self.policy,1,self.compute)
        row=db.session.get(ApprovalViewCache,(1,1));row.expires_at=time.time()-1;db.session.commit()
        def failed():raise ApprovalError('原生服务无法核验')
        with self.assertRaisesRegex(ApprovalError,'无法核验'):display_status(self.policy,1,failed)
        with self.assertRaisesRegex(ApprovalError,'无法核验'):display_status(self.policy,1,self.compute)
        self.assertEqual(self.calls,1)

    def test_crashed_worker_lease_recovers_without_stale_publication(self):
        db.session.add(ApprovalViewCache(policy_id=1,number=1,context_hash=context_hash(self.policy),
            lease='旧进程',lease_until=time.time()-1,expires_at=0,payload='{}'));db.session.commit()
        self.assertTrue(display_status(self.policy,1,self.compute)['satisfied'])
        row=db.session.get(ApprovalViewCache,(1,1));row.expires_at=0;db.session.commit()
        def replaced():
            row=db.session.get(ApprovalViewCache,(1,1));row.lease='其他读取者';db.session.commit()
            return self.compute()
        with self.assertRaisesRegex(ApprovalError,'租约已失效'):display_status(self.policy,1,replaced)

    def test_waiting_then_calculation_cannot_restart_the_request_budget(self):
        class Clock:
            now=1000.0
            def monotonic(self):return self.now
            def time(self):return self.now
            def sleep(self,seconds):self.now+=seconds
        clock=Clock()
        db.session.add(ApprovalViewCache(policy_id=1,number=1,context_hash=context_hash(self.policy),
            lease='等待旧读取者',lease_until=1008.8,expires_at=0,payload='{}'));db.session.commit()
        def delayed():
            clock.sleep(2)
            return {'satisfied':True,'checked_at':clock.time(),'valid_until':clock.time()+15}
        with patch('approval.display_cache.time',clock):
            with self.assertRaisesRegex(ApprovalError,'预算|超时|租约'):
                display_status(self.policy,1,delayed)
        row=db.session.get(ApprovalViewCache,(1,1),populate_existing=True)
        self.assertFalse(row and json.loads(row.payload).get('value',{}).get('satisfied'))

    def test_caller_budget_stops_waiting_before_default_lease_timeout(self):
        class Clock:
            now=1000.0
            def monotonic(self):return self.now
            def time(self):return self.now
            def sleep(self,seconds):self.now+=seconds
        clock=Clock()
        db.session.add(ApprovalViewCache(policy_id=1,number=1,context_hash=context_hash(self.policy),
            lease='其他有效读取',lease_until=1008,expires_at=0,payload='{}'));db.session.commit()
        with patch('approval.display_cache.time',clock),self.assertRaises(ApprovalError):
            display_status(self.policy,1,self.compute,deadline=1000.1)
        self.assertLess(clock.now,1000.13)
        self.assertEqual(self.calls,0)
        self.assertEqual(db.session.get(ApprovalViewCache,(1,1)).lease,'其他有效读取')

    def test_expired_request_cannot_return_even_a_fresh_shared_snapshot(self):
        display_status(self.policy,1,self.compute)
        with self.assertRaises(ApprovalError):
            display_status(self.policy,1,self.compute,deadline=time.monotonic()-1)
        self.assertEqual(self.calls,1)

    def test_publish_commit_crossing_deadline_cannot_leave_reusable_success(self):
        class Clock:
            now=1000.0
            def monotonic(self):return self.now
            def time(self):return self.now
        clock=Clock();commit=db.session.commit;commits=0
        def delayed_commit():
            nonlocal commits
            commits+=1
            if commits==2:clock.now+=.2
            commit()
        with patch('approval.display_cache.time',clock),patch.object(db.session,'commit',delayed_commit):
            with self.assertRaises(ApprovalError):
                display_status(self.policy,1,self.compute,deadline=1000.1)
        row=db.session.get(ApprovalViewCache,(1,1),populate_existing=True)
        self.assertLessEqual(row.expires_at,clock.now)
        with patch('approval.display_cache.time',clock):
            display_status(self.policy,1,self.compute)
        self.assertEqual(self.calls,2)

    def test_size_and_total_rows_are_bounded(self):
        with patch('approval.display_cache.MAX_ROWS',2):
            display_status(self.policy,1,self.compute);display_status(self.policy,2,self.compute)
            with self.assertRaisesRegex(ApprovalError,'容量上限'):display_status(self.policy,3,self.compute)
            self.assertEqual(ApprovalViewCache.query.count(),2)
        with patch('approval.display_cache.MAX_PAYLOAD',256),self.assertRaisesRegex(ApprovalError,'大小上限'):
            display_status(self.policy,4,lambda:{'satisfied':True,'data':'测试'*500})
        self.assertLess(len(db.session.get(ApprovalViewCache,(1,4)).payload.encode()),256)

    def test_two_processes_share_one_calculation(self):
        event=multiprocessing.Event();counter=multiprocessing.Value('i',0);queue=multiprocessing.Queue()
        workers=[multiprocessing.Process(target=worker,args=(str(self.path),event,counter,queue)) for _ in range(2)]
        for process in workers:process.start()
        event.set()
        try:
            results=[queue.get(timeout=8) for _ in workers]
            self.assertTrue(all(r.get('satisfied') is True for r in results),results)
            self.assertEqual(counter.value,1)
        finally:
            for process in workers:
                process.join(3)
                if process.is_alive():process.terminate();process.join(3)

    def test_separate_view_service_has_no_management_or_hook_routes(self):
        from approval.view_app import create_app
        with patch('approval.view_app.DATABASE_URL','sqlite:///'+str(self.path)):
            client=create_app().test_client()
        self.assertEqual(client.get('/health').json,{'ok':True,'role':'approval-view'})
        for path in ('/api/session','/api/approval-rules/policies','/api/approval-internal/1/check'):
            self.assertEqual(client.get(path).status_code,404)
            self.assertEqual(client.post(path,json={}).status_code,404)
        self.assertEqual(client.get('/api/approval-view/1/2/1').status_code,404)  # 没有实例不能开始核验。
        with patch.dict('os.environ',{'GM_UPGRADE_MAINTENANCE':'1'}):
            self.assertEqual(client.get('/api/approval-view/1/2/1').status_code,503)
            self.assertEqual(client.get('/health').status_code,200)


if __name__=='__main__':unittest.main(verbosity=2)
