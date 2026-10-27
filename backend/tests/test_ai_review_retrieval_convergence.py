"""用单文件分页、广泛命中、交替重复和中文输入复现检索放大；协议替身不代表模型质量。"""
import contextlib
import json
import time
import unittest
from unittest.mock import patch

import test_ai_review_repository_context as repository_tests
from ai_review.common import ReviewError, encode
from ai_review.context import source
from ai_review.engine import review, invoke, capability
from ai_review.broker import create_broker
from ai_review.budgets import check_input, MAX_MODEL_INPUT_BYTES


class ConvergenceTests(unittest.TestCase):
    setUp=repository_tests.RepositoryContextTests.setUp
    enqueue=repository_tests.RepositoryContextTests.enqueue
    fixture=repository_tests.RepositoryContextTests.fixture

    def test_r26_schema_upgrade_preserves_records_and_is_repeatable(self):
        from ai_review.store import Store
        job,snap,item,client,reader=self.fixture()
        config=self.store.config()
        with self.store.tx() as c:
            c.execute('ALTER TABLE review_turns DROP COLUMN created')
            c.execute('PRAGMA user_version=3')
        for _ in range(2):
            self.store=Store(self.store.path)
            self.assertEqual(self.store.config(),config)
            with contextlib.closing(self.store.connect()) as c:
                self.assertIn('created',[r[1] for r in c.execute('PRAGMA table_info(review_turns)')])
                self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0],18)
        result=review(self.store,job,config,snap,[item],caller=lambda *args:{'findings':[]},client=client)
        self.assertTrue(result['coverage']['complete'])

    def test_completed_model_result_does_not_force_read_all_search_hits(self):
        job,snap,item,client,reader=self.fixture({'lib/driver.c':'\n'.join('int LIMIT_'+str(i)+';' for i in range(7000))})
        calls=[]
        def model(*args):
            calls.append(args[-1])
            self.assertLessEqual(len(calls),2,'模型已经给出结论，流水线仍强制继续读取搜索命中')
            if len(calls)==1:return {'context_requests':[{'op':'search_code','version':'after','query':'LIMIT','pattern':'*.c'}]}
            return {'findings':[]}
        result=review(self.store,job,self.store.config(),snap,[item],caller=model,client=client)
        self.assertEqual(len(calls),2)
        self.assertEqual(result['context_retrieval']['rounds'],1)
        self.assertTrue(all(r['request']['op']=='search_code' for r in reader.records(result['evidence'][0]['unit_id'])))

    def test_322_read_pages_are_one_model_lookup_and_bounded_input(self):
        job,snap,item,client,reader=self.fixture({'lib/driver.c':'\n'.join('int value_'+str(i)+';' for i in range(64400))})
        calls=[]
        def model(*args):
            payload=args[-1];calls.append(payload)
            self.assertLess(len(encode(payload).encode()),MAX_MODEL_INPUT_BYTES)
            if len(calls)==1:return {'context_requests':[{'op':'read_file','version':'after','path':'lib/driver.c','start_line':1,'end_line':64400}]}
            return {'findings':[]}
        result=review(self.store,job,self.store.config(),snap,[item],caller=model,client=client)
        self.assertEqual(len(calls),2)
        self.assertEqual(result['context_retrieval']['operations'],322)
        self.assertEqual(result['context_retrieval']['model_rounds'],1)
        self.assertEqual(len(client.reads),1)
        self.assertGreater(calls[-1]['context_omitted_count'],0)
        self.assertEqual(calls[0]['before'],calls[-1]['before'])
        self.assertEqual(calls[0]['after'],calls[-1]['after'])
        self.assertFalse(result['coverage']['complete'])

    def test_alternating_overlapping_requests_stop_without_replaying_on_resume(self):
        job,snap,item,client,reader=self.fixture({'lib/driver.c':'\n'.join('int value_'+str(i)+';' for i in range(100))})
        calls=[]
        def model(*args):
            payload=args[-1];calls.append(payload)
            self.assertLessEqual(len(calls),8,'交替请求没有新证据却持续调用模型')
            if 'candidates' in payload:return {'checks':[]}
            n=len(calls)%2
            return {'findings':[{'title':'候选意见','explanation':'需要边界确认'}],
                    'context_requests':[{'op':'read_file','version':'after','path':'lib/driver.c','start_line':1+n,'end_line':100-n}]}
        result=review(self.store,job,self.store.config(),snap,[item],caller=model,client=client)
        self.assertTrue(result['findings']);self.assertFalse(result['coverage']['complete'])
        count=len(calls)
        review(self.store,job,self.store.config(),snap,[item],caller=model,client=client)
        self.assertEqual(count,len(calls))

    def test_unit_time_budget_does_not_restart_when_batch_changes(self):
        job,snap,item,client,reader=self.fixture();calls=[]
        config=dict(self.store.config(),task_timeout_seconds=60,model_request_timeout_seconds=60)
        def model(*args):
            calls.append(1)
            with self.store.tx() as c:c.execute('UPDATE review_turns SET created=? WHERE job=?',(time.time()-61,job['id']))
            return {'findings':[{'title':'保留初步意见','explanation':'需要更多依据'}],
                    'context_requests':[{'op':'read_file','version':'after','path':'lib/limit.h'}]}
        result=review(self.store,job,config,snap,[item],caller=model,client=client)
        self.assertEqual(len(calls),1);self.assertEqual(client.reads,[])
        self.assertTrue(result['findings']);self.assertFalse(result['coverage']['complete'])

    def test_unicode_evidence_is_bounded_in_bytes_and_preserves_change(self):
        job,snap,item,client,reader=self.fixture({'lib/driver.c':'中'*10000})
        item['context']=[source(snap,'lib/original.h','before','相关声明','文'*9000,1)]
        reader.perform('中文',0,[{'op':'read_file','version':'after','path':'lib/driver.c'}])
        actual=reader.augment(item,'中文')
        self.assertLess(len(encode(actual).encode()),48000)
        self.assertEqual(actual['before'],item['before']);self.assertEqual(actual['after'],item['after'])
        self.assertTrue(actual.get('context_omitted'))

    def test_full_request_budget_prevents_upstream_and_preserves_diagnostic(self):
        job,snap,item,client,reader=self.fixture()
        with patch('ai_review.engine.requests.Session') as session:
            with self.assertRaises(ReviewError) as error:invoke(self.store,job,self.store.config(),'审查',{'内容':'中'*30000})
            session.assert_not_called()
        self.assertEqual(error.exception.error_code,'MODEL_INPUT_BUDGET')
        diagnostic=self.store.job(job['id'])['diagnostic']
        self.assertGreater(diagnostic['input_bytes'],MAX_MODEL_INPUT_BYTES)
        token=capability(self.store,job,self.store.config()['model'])
        with patch('ai_review.broker.fetch_model') as fetch:
            response=create_broker(self.store).test_client().post('/v1/chat/completions',headers={'Authorization':'Bearer '+token},
                json={'model':self.store.config()['model'],'messages':[{'role':'user','content':'中'*30000}]})
            self.assertEqual(response.status_code,413);fetch.assert_not_called()
        self.assertEqual(self.store.job(job['id'])['diagnostic']['error_code'],'MODEL_INPUT_BUDGET')


if __name__=='__main__':unittest.main()
