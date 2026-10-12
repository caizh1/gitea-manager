"""性能优化必须保持原文、顺序、缺口和恢复语义；计时不能成为终止条件。"""
import contextlib
import copy
import random
import sqlite3
import unittest
from unittest.mock import patch

import test_ai_review_repository_context as fixtures
from ai_review.common import encode, Stale
from ai_review.context import source
from ai_review.performance import record, summary, span
from ai_review.progress_state import job_progress
from ai_review.store import Store


def full_serialization_budget(item,receipts):
    """独立保留旧版逐次完整编码的预算算法，核对字节边界与顺序。"""
    value={k:v for k,v in item.items() if k not in ('context_results','context_omitted','context_omitted_count')}
    value.update(context=[],header_needs=[],context_gaps=[])
    metadata=[]
    for receipt in receipts[-8:]:
        entry={k:v for k,v in receipt.items() if k!='source'}
        if len(entry.get('matches',[]))>10:entry.update(matches=entry['matches'][:10],display_limited=True)
        metadata.append(entry)
    seen=set();omitted=[]
    for src in [r['source'] for r in reversed(receipts) if r.get('source')]+item['context']:
        if src['source_id'] in seen:continue
        value['context'].append(src);seen.add(src['source_id'])
        if len(encode(value).encode())>44000:value['context'].pop();omitted.append(src['source_id'])
    value['context_results']=[]
    for entry in reversed(metadata):
        value['context_results'].insert(0,entry)
        if len(encode(value).encode())>47000:
            value['context_results'].pop(0)
            compact={k:v for k,v in entry.items() if k not in ('matches','paths','skipped')};compact['display_limited']=True
            value['context_results'].insert(0,compact)
            if len(encode(value).encode())>47000:value['context_results'].pop(0)
    if omitted:
        ids=list(dict.fromkeys(omitted));value.update(context_omitted=ids[:16],context_omitted_count=len(ids))
    return value


class PerformanceTests(unittest.TestCase):
    setUp=fixtures.RepositoryContextTests.setUp
    enqueue=fixtures.RepositoryContextTests.enqueue
    fixture=fixtures.RepositoryContextTests.fixture

    def test_byte_budget_matches_full_serialization_for_unicode_crlf_and_duplicates(self):
        _,snap,item,_,reader=self.fixture();randomizer=random.Random(20260916)
        item.update(header_needs=[],context_gaps=[])
        for case in range(80):
            receipts=[]
            for n in range(randomizer.randrange(1,90)):
                src=source(snap,'路径/文件'+str(n%37)+'.h','before' if n%2 else 'after','声明',
                           ('中文\\"\r\n' if n%3 else 'x')*randomizer.randrange(1,7000),n+1)
                receipts.append({'source':src,'complete':True,'request_id':str(n),'matches':['搜索'*300]*12})
            item['context']=[r['source'] for r in receipts[:8]]
            if case%5==0:receipts.extend(receipts[:8])
            with patch.object(reader,'records',return_value=receipts):actual=reader.augment(item,'单元')
            self.assertEqual(actual,full_serialization_budget(item,receipts),f'随机组合 {case}')

    def test_322_pages_scan_receipts_once_and_resume_without_file_reads(self):
        job,snap,item,client,reader=self.fixture({'lib/driver.c':'\n'.join('int x'+str(i)+';' for i in range(64400))})
        request={'op':'read_file','version':'after','path':'lib/driver.c','start_line':1,'end_line':64400}
        with patch.object(reader,'records',wraps=reader.records) as scans:
            values=reader.perform('单元',0,[request]);self.assertEqual(len(values),322);self.assertEqual(scans.call_count,1)
        with patch.object(reader,'execute',side_effect=AssertionError('不得重读已确认分页')):
            self.assertEqual(reader.perform('单元',0,[request]),values)
            self.assertEqual(reader.perform('单元',1,[request]),values)
        self.assertFalse(reader.gaps(item,'单元'));self.assertEqual(len(client.reads),1)
        self.assertGreater(summary(self.store,job['id'])['analysis_detail_seconds']['retrieval'],0)

    def test_interval_merge_preserves_holes_versions_targets_and_errors(self):
        _,_,item,_,reader=self.fixture({'a.c':'x\n'*800,'b.c':'x\n'*800})
        def req(start,end,path='a.c',version='after'):
            return dict(op='read_file',path=path,version=version,start_line=start,end_line=end)
        with patch.object(reader,'next_request',return_value=None):
            reader.perform('缺页',0,[req(1,800),req(401,800),req(201,400,'b.c'),req(201,400,version='before')])
            self.assertTrue(reader.gaps(item,'缺页'))
            reader.perform('缺页',1,[req(201,400),req(601,800)])
            self.assertFalse(reader.gaps(item,'缺页'))
            reader.perform('缺页',2,[req(1,2,'missing.c')])
            self.assertEqual(len(reader.gaps(item,'缺页')),1)

    def test_r28_migration_preserves_config_history_and_is_idempotent(self):
        job,*_=self.fixture();config=self.store.config();before=self.store.job(job['id'])
        with self.store.tx() as c:c.execute('DROP TABLE review_performance');c.execute('PRAGMA user_version=4')
        for _ in range(2):
            store=Store(self.store.path);self.assertEqual(store.config(),config)
            self.assertEqual({k:v for k,v in store.job(job['id']).items() if k!='progress'},
                             {k:v for k,v in before.items() if k!='progress'})
            self.assertIsNone(store.job(job['id'],public=True)['performance'])
            with contextlib.closing(store.connect()) as c:self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],13)

    def test_nested_durations_are_not_added_and_survive_restart(self):
        job,*_=self.fixture();key=job['id']
        for category,call,seconds in [('preparation','',2),('analysis','',710),('publication','',3),
                ('retrieval','',5),('call','甲',700),('executor','甲',699),('model','甲',650)]:
            record(self.store,key,category,call,seconds)
        result=summary(Store(self.store.path),key)
        self.assertEqual(result['stages_seconds'],dict(preparation=2,analysis=710,publication=3))
        self.assertEqual(result['analysis_detail_seconds']['overhead'],50)
        self.assertEqual(result['calls'][0]['overhead_seconds'],50)
        record(self.store,key,'call','乙',1)
        result=summary(self.store,key);self.assertIsNone(result['analysis_detail_seconds']['overhead'])
        self.assertFalse(result['call_breakdown_complete'])

    def test_failure_recording_cannot_replace_original_exception(self):
        job,*_=self.fixture()
        with patch.object(self.store,'tx',side_effect=sqlite3.OperationalError('不可用')):
            with self.assertLogs(level='WARNING'),self.assertRaisesRegex(Stale,'原始取消'):
                with span(self.store,job['id'],'retrieval'):raise Stale('原始取消')

    def test_target_excludes_queue_and_is_never_a_failure(self):
        job,*_=self.fixture();job.update(started_at=5000,created=1000,lease=9000,stage='继续分析')
        for elapsed in (600,601,2300):
            progress=job_progress(job,5000+elapsed)
            self.assertEqual(progress['target_exceeded'],elapsed>600)
            self.assertEqual(progress['queue_seconds'],4000)
            self.assertEqual(progress['status'],'running');self.assertEqual(progress['label'],'正在审查')
        job.update(status='succeeded',finished_at=5715)
        self.assertEqual(job_progress(job,10000)['elapsed_seconds'],715)

    def test_metrics_expose_only_numbers_and_call_identifiers(self):
        job,*_=self.fixture();record(self.store,job['id'],'call','调用编号',1)
        text=encode(self.store.job(job['id'],public=True)['performance'])
        for secret in ('api_key','prompt','capability','source','messages'):self.assertNotIn(secret,text)

    def test_interrupted_stage_is_not_reported_as_complete_after_resume(self):
        job,*_=self.fixture();key=job['id']
        record(self.store,key,'started:analysis','',0)
        # 上次进程被终止，没有收尾；新进程只能保存本次可测量的部分。
        with span(Store(self.store.path),key,'analysis'):pass
        value=summary(self.store,key)
        self.assertIsNone(value['stages_seconds']['analysis'])
        self.assertEqual(value['incomplete_stages'],['analysis'])
