"""展示租约的确定性并发、代际隔离与到期恢复；仅使用临时合成数据库。"""
import json
import multiprocessing as mp
import os
from pathlib import Path
import queue
import tempfile
import threading
import time as real_time
import unittest
from unittest.mock import patch

from flask import Flask
from sqlalchemy import event
from models import db, ApprovalPolicy, ApprovalViewCache
from approval import display_cache as cache
from approval.rules import ApprovalError, digest

CTX = mp.get_context('spawn')


def app_for(path):
    app = Flask('合成租约测试')
    app.config.update(SQLALCHEMY_DATABASE_URI='sqlite:///'+str(path), SQLALCHEMY_TRACK_MODIFICATIONS=False)
    db.init_app(app)
    return app


class SignallingClock:
    def __init__(self, phases): self.phases = phases
    def time(self): return real_time.time()
    def monotonic(self): return real_time.monotonic()
    def sleep(self, seconds):
        phase = self.phases.get(threading.current_thread().name)
        if phase is not None: phase.set()
        real_time.sleep(seconds)


class ShiftClock:
    def __init__(self): self.shift = 0
    def time(self): return real_time.time()+self.shift
    def monotonic(self): return real_time.monotonic()+self.shift
    def sleep(self, seconds): real_time.sleep(seconds)


def actor(path, index, generation, phases, release, metrics, guard, results, atomic=None, process=False):
    name = '读取'+str(index)
    threading.current_thread().name = name
    cache.time = SignallingClock({'读取'+str(i): e for i,e in enumerate(phases)})
    app = app_for(path)
    used = False
    def on_select(connection, cursor, statement, params, context, many):
        nonlocal used
        if atomic is not None and not used and statement.lstrip().upper().startswith('SELECT') and 'approval_view_cache' in statement:
            used = True
            atomic.wait(4)
    with app.app_context():
        engine = db.engine
        if atomic is not None: event.listen(engine, 'after_cursor_execute', on_select)
        try:
            policy = db.session.get(ApprovalPolicy, 1)
            def calculate():
                with guard:
                    metrics['active'].value += 1
                    metrics['total'].value += 1
                    metrics['maximum'].value = max(metrics['maximum'].value, metrics['active'].value)
                phases[index].set()
                try:
                    if not release.wait(5): raise RuntimeError('计算暂停点未释放')
                    return {'satisfied': True, 'proof': generation, 'checked_at': real_time.time(),
                            'valid_until': real_time.time()+15}
                finally:
                    with guard: metrics['active'].value -= 1
            value = cache.display_status(policy, 1, calculate, generation=generation)
            results.put({'读取': index, '返回': True, '内容代际': value['proof']})
        except Exception as exc:
            results.put({'读取': index, '返回': False, '异常': type(exc).__name__, '说明': str(exc)})
        finally:
            if atomic is not None: event.remove(engine, 'after_cursor_execute', on_select)
            db.session.remove(); engine.dispose()


def crash_actor(path, entered):
    with app_for(path).app_context():
        policy = db.session.get(ApprovalPolicy, 1)
        def calculate():
            entered.set()
            os._exit(72)  # 只终止本测试子进程，保留已提交的计算租约。
        cache.display_status(policy, 1, calculate, generation=['同一状态'])


class DisplayLeaseConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='approval-lease-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'合成审批.db'
        self.app = app_for(self.path)
        self.context = self.app.app_context(); self.context.push(); db.create_all()
        self.policy = ApprovalPolicy(id=1,server_id=1,repo_id=2,owner_id=3,repo_name='合成/仓库',rule_json='{}')
        db.session.add(self.policy); db.session.commit()
        self.original_clock = cache.time

    def tearDown(self):
        cache.time = self.original_clock
        engine = db.engine; db.session.remove(); self.context.pop(); engine.dispose()

    def paired(self, generations, process=False, atomic=False):
        count = len(generations)
        events = [CTX.Event() for _ in generations] if process else [threading.Event() for _ in generations]
        release = CTX.Event() if process else threading.Event()
        guard = CTX.Lock() if process else threading.Lock()
        metrics = {name:CTX.Value('i',0) for name in ('active','maximum','total')}
        results = CTX.Queue() if process else queue.Queue()
        barrier = threading.Barrier(count) if atomic else None
        workers=[]; returned=[]; cleanup=[]
        try:
            for i,generation in enumerate(generations):
                args=(str(self.path),i,generation,events,release,metrics,guard,results,barrier,process)
                worker=CTX.Process(target=actor,args=args) if process else threading.Thread(target=actor,args=args,daemon=True)
                workers.append(worker); worker.start()
                if not atomic: self.assertTrue(events[i].wait(4),'未到达计算或等待观察点')
            if atomic:
                for phase in events: self.assertTrue(phase.wait(4),'并发领取未到达观察点')
            release.set()
            for _ in generations: returned.append(results.get(timeout=6))
        finally:
            release.set()
            for index, worker in enumerate(workers):
                worker.join(3)
                forced = process and worker.is_alive()
                if forced:
                    worker.terminate(); worker.join(2)
                cleanup.append({'序号': index, '需要强制清理': bool(forced), '仍运行': worker.is_alive(),
                                '退出码': worker.exitcode if process else None})
            alive=any(w.is_alive() for w in workers)
            if process: results.close(); results.join_thread()
            self.assertFalse(any(item['需要强制清理'] or item['仍运行'] for item in cleanup), cleanup)
            if process:
                self.assertEqual([item['退出码'] for item in cleanup], [0]*len(workers), cleanup)
        returned.sort(key=lambda x:x['读取'])
        observed={'模式':'双进程' if process else '线程','请求数':count,'最大同时计算':metrics['maximum'].value,
                  '总计算':metrics['total'].value,'返回':returned,'全部退出':not alive,'强制同时读取空记录':atomic,'收尾':cleanup}
        self.assertFalse(alive,'自有测试执行单元未退出')
        self.assertEqual(metrics['maximum'].value,1,observed)
        self.assertTrue(all(x['返回'] for x in returned),observed)
        self.assertEqual([x['内容代际'] for x in returned],generations,'不得借用其他代际的快照')
        if all(g==generations[0] for g in generations): self.assertEqual(metrics['total'].value,1)
        else: self.assertGreaterEqual(metrics['total'].value,len({str(g) for g in generations}))

    def test_thread_same_generation(self): self.paired([['A'],['A']])
    def test_thread_new_then_old(self): self.paired([['新'],['旧']])
    def test_thread_old_then_new(self): self.paired([['旧'],['新']])
    def test_thread_a_b_a(self): self.paired([['A'],['B'],['A']])
    def test_process_same_generation(self): self.paired([['A'],['A']],process=True)
    def test_process_new_then_old(self): self.paired([['新'],['旧']],process=True)
    def test_process_old_then_new(self): self.paired([['旧'],['新']],process=True)
    def test_atomic_same_generation(self): self.paired([['A'],['A']],atomic=True)
    def test_atomic_different_generation(self): self.paired([['新'],['旧']],atomic=True)

    def change_during_compute(self, field):
        def compute():
            setattr(self.policy,field,getattr(self.policy,field)+1);db.session.commit()
            return {'satisfied':True,'valid_until':real_time.time()+15}
        with self.assertRaises(ApprovalError): cache.display_status(self.policy,1,compute)
        row=db.session.get(ApprovalViewCache,(1,1),populate_existing=True)
        self.assertFalse(row and json.loads(row.payload).get('value',{}).get('satisfied'))
    def test_rule_change_during_compute(self): self.change_during_compute('revision')
    def test_instance_identity_change_during_compute(self): self.change_during_compute('server_id')
    def test_repository_identity_change_during_compute(self): self.change_during_compute('repo_id')

    def test_waiter_deadline_keeps_other_generation_lease(self):
        expected=digest([cache.context_hash(self.policy),['新']])
        db.session.add(ApprovalViewCache(policy_id=1,number=1,context_hash=expected,lease='当前计算',
                                         lease_until=real_time.time()+8,expires_at=0,payload='{}'));db.session.commit()
        called=[];began=real_time.monotonic()
        def compute(): called.append(True);return {'satisfied':True,'valid_until':real_time.time()+15}
        try:
            with self.assertRaises(ApprovalError):
                cache.display_status(self.policy,1,compute,generation=['旧'],deadline=real_time.monotonic()+.12)
        finally:
            row=db.session.get(ApprovalViewCache,(1,1),populate_existing=True)
        self.assertFalse(called);self.assertEqual(row.lease,'当前计算')
        self.assertLess(real_time.monotonic()-began,.5)

    def test_rule_change_while_waiting(self):
        db.session.add(ApprovalViewCache(policy_id=1,number=1,context_hash=cache.context_hash(self.policy),
                                         lease='原计算',lease_until=real_time.time()+8,expires_at=0,payload='{}'));db.session.commit()
        policy=self.policy
        class ChangingClock(ShiftClock):
            def sleep(inner,seconds):
                policy.revision+=1;db.session.commit()
        with patch.object(cache,'time',ChangingClock()),self.assertRaisesRegex(ApprovalError,'接入配置变化'):
            cache.display_status(self.policy,1,lambda:self.fail('规则变化不能开始计算'))
        self.assertEqual(db.session.get(ApprovalViewCache,(1,1)).lease,'原计算')

    def test_crashed_process_lease_and_expiry(self):
        entered=CTX.Event();child=CTX.Process(target=crash_actor,args=(str(self.path),entered))
        child.start()
        try:
            self.assertTrue(entered.wait(4));child.join(3);self.assertEqual(child.exitcode,72)
        finally:
            forced = child.is_alive()
            if forced: child.terminate(); child.join(2)
            self.assertFalse(forced, '崩溃样本必须自行以指定退出码结束，不能依靠强制清理通过')
            self.assertFalse(child.is_alive())
        before=db.session.get(ApprovalViewCache,(1,1),populate_existing=True).lease
        with self.assertRaises(ApprovalError):
            cache.display_status(self.policy,1,lambda:self.fail('未到期不能接管'),generation=['同一状态'],deadline=real_time.monotonic()+.1)
        self.assertEqual(db.session.get(ApprovalViewCache,(1,1),populate_existing=True).lease,before)
        clock=ShiftClock();clock.shift=10
        with patch.object(cache,'time',clock):
            value=cache.display_status(self.policy,1,lambda:{'satisfied':True,'valid_until':clock.time()+15},generation=['同一状态'])
        self.assertTrue(value['satisfied'])

    def test_late_owner_cannot_delete_replacement(self):
        clock=ShiftClock();entered=threading.Event();release=threading.Event();returned=[]
        def owner():
            with app_for(self.path).app_context():
                policy=db.session.get(ApprovalPolicy,1)
                def compute():
                    entered.set()
                    if not release.wait(4):raise RuntimeError('旧持有者暂停未释放')
                    return {'satisfied':True,'proof':'旧持有者','valid_until':clock.time()+15}
                try:cache.display_status(policy,1,compute,generation=['旧']);returned.append('错误返回')
                except ApprovalError as exc:returned.append(str(exc))
                finally:
                    engine=db.engine;db.session.remove();engine.dispose()
        with patch.object(cache,'time',clock):
            worker=threading.Thread(target=owner,daemon=True);worker.start()
            try:
                self.assertTrue(entered.wait(3));clock.shift=10
                value=cache.display_status(self.policy,1,lambda:{'satisfied':True,'proof':'新持有者','valid_until':clock.time()+15},generation=['新'])
                self.assertEqual(value['proof'],'新持有者')
            finally:release.set();worker.join(3)
        self.assertFalse(worker.is_alive());self.assertEqual(len(returned),1);self.assertIn('预算',returned[0])
        row=db.session.get(ApprovalViewCache,(1,1),populate_existing=True)
        self.assertEqual(json.loads(row.payload)['value']['proof'],'新持有者')


if __name__ == '__main__':
    unittest.main(verbosity=2)
