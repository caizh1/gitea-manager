"""新旧结束语义、统一额度、可恢复对象和失败单元隔离的固定验收。"""
import contextlib
import json
import unittest
from unittest.mock import patch

from ai_review.completion import ResponseCollector, CompletionError, interpret, recover_objects
from ai_review.broker import validate_completion, create_broker
from ai_review.engine import capability, review
from ai_review.common import ReviewError, Stale
import test_ai_review_repository_context as repository_tests


def stream(text, reason='stop', done=True, tools=None):
    values=[{'choices':[{'index':0,'delta':dict(content=text, **({'tool_calls':tools} if tools else {})),'finish_reason':None}]},
            {'choices':[{'index':0,'delta':{},'finish_reason':reason}]}, {'choices':[],'usage':{'completion_tokens':9000}}]
    return (''.join('data: '+json.dumps(v)+'\r\n\r\n' for v in values)+('data: [DONE]\n\n' if done else '')).encode()


class CompletionTests(unittest.TestCase):
    def test_stop_without_done_and_repeated_done_are_compatible(self):
        for raw in (stream('{"findings":[]}',done=False),stream('{"findings":[]}')+b'data: [DONE]\n\n'):
            value,meta=validate_completion(raw,True)
            self.assertEqual(value,{'findings':[]});self.assertTrue(meta['completion_complete'])
            self.assertEqual(meta['usage']['completion_tokens'],9000)

    def test_length_and_missing_reason_preserve_objects_but_never_claim_complete(self):
        for reason in ('length',None,'content_filter'):
            value,meta=validate_completion(stream('{"findings":[{"title":"问题","explanation":"依据"}]}',reason),True)
            self.assertEqual(len(value['findings']),1);self.assertFalse(meta['completion_complete'])
            self.assertTrue(meta['completion_warnings'])

    def test_partial_json_recovers_only_finished_top_level_items(self):
        text='{"findings":[{"title":"完整","explanation":"说明中的 { 不改变边界"},{"title":"未完整'
        value,meta=validate_completion(stream(text,'length'),True)
        self.assertEqual(len(value['findings']),1);self.assertEqual(value['findings'][0]['title'],'完整')
        self.assertFalse(meta['completion_complete']);self.assertIsInstance(meta['json_offset'],int)
        self.assertEqual(recover_objects('{"note":"fake \\\"findings\\\":[{\\\"title\\\":\\\"假\\\"}]'),{})

    def test_no_usable_result_has_specific_diagnostic(self):
        with self.assertRaises(CompletionError) as caught:validate_completion(stream('{"findings":[{"title":"未完整','length'),True)
        self.assertEqual(caught.exception.code,'no_usable_result')
        self.assertEqual(caught.exception.metadata['finish_reasons'],['length'])
        self.assertIsInstance(caught.exception.metadata['json_offset'],int)

    def test_only_explicit_readonly_tools_are_converted(self):
        tool={'index':0,'function':{'name':'read_file','arguments':json.dumps({'version':'before','path':'code.c','start_line':1,'end_line':500})}}
        value,meta=validate_completion(stream('',reason='tool_calls',tools=[tool]),True)
        self.assertEqual(value['context_requests'][0]['op'],'read_file')
        self.assertEqual(value['context_requests'][0]['version'],'before')
        self.assertTrue(meta['completion_complete'])
        tool['function']['name']='shell'
        with self.assertRaises(CompletionError):validate_completion(stream('',reason='tool_calls',tools=[tool]),True)
        value,meta=validate_completion(stream('{"findings":[]}',reason='tool_calls',tools=[tool]),True)
        self.assertNotIn('context_requests',value);self.assertFalse(meta['completion_complete'])

    def test_large_heartbeat_stream_does_not_count_as_body(self):
        collector=ResponseCollector(True,1024)
        for _ in range(20000):collector.feed(b': '+b'x'*80+b'\n\n')
        collector.feed(stream('{"findings":[]}'))
        envelope,meta=collector.finish()
        self.assertEqual(meta['body_bytes'],15)
        self.assertEqual(interpret(envelope,meta)[0],{'findings':[]})

    def test_over_one_mib_body_is_supported_and_configured_limit_is_enforced(self):
        text=json.dumps({'findings':[{'title':'长说明','explanation':'x'*(1024*1024+1)}]})
        value,meta=validate_completion(stream(text),True)
        self.assertGreater(len(value['findings'][0]['explanation']),1024*1024)
        with self.assertRaises(CompletionError) as caught:validate_completion(stream(text),True,1024*1024)
        self.assertEqual(caught.exception.code,'response_body_limit')


class RuntimeTests(unittest.TestCase):
    setUp=repository_tests.RepositoryContextTests.setUp
    enqueue=repository_tests.RepositoryContextTests.enqueue
    fixture=repository_tests.RepositoryContextTests.fixture
    def test_more_than_six_distinct_rounds_and_large_read_are_completed(self):
        files={f'lib/p{i}.h':'\n'.join(f'int row{j};' for j in range(501)) for i in range(9)}
        job,snap,item,client,reader=self.fixture(files);calls=[]
        def model(*args):
            calls.append(1)
            if len(calls)<=9:return {'context_requests':[{'op':'read_file','version':'after','path':f'lib/p{len(calls)-1}.h','start_line':1,'end_line':501}]}
            return {'findings':[]}
        result=review(self.store,job,self.store.config(),snap,[item],caller=model,client=client)
        self.assertEqual(len(calls),10);self.assertEqual(len(client.reads),9)
        self.assertEqual(result['context_retrieval']['operations'],27)
        receipts=reader.records(result['evidence'][0]['unit_id'])
        for path in files:
            rows=[r for r in receipts if r['request']['path']==path]
            self.assertEqual([r['source']['start'] for r in rows],[1,201,401])
            self.assertEqual(rows[-1]['source']['end'],501)

    def test_failure_in_middle_continues_and_resume_does_not_replay(self):
        job,snap,item,client,_=self.fixture()
        items=[dict(item,scope=dict(item['scope'],name=f'函数{i}')) for i in range(3)];calls=[]
        def model(*args):
            calls.append(1)
            if len(calls)==2:raise ReviewError('本单元响应损坏')
            return {'findings':[]}
        result=review(self.store,job,self.store.config(),snap,items,caller=model,client=client)
        self.assertEqual(len(calls),3);self.assertEqual(result['units_failed'],1)
        self.assertEqual(result['units_processed'],3);self.assertFalse(result['coverage']['complete'])
        review(self.store,job,self.store.config(),snap,items,caller=model,client=client)
        self.assertEqual(len(calls),3)

    def test_credentials_or_version_change_still_stops_following_units(self):
        job,snap,item,client,_=self.fixture();calls=[]
        def model(*args):calls.append(1);raise Stale('权限已撤销')
        with self.assertRaises(Stale):review(self.store,job,self.store.config(),snap,[item,dict(item,path='next.c')],caller=model,client=client)
        self.assertEqual(len(calls),1)

    def test_budget_only_edits_freeze_new_fields_and_keep_old_job_active(self):
        job,_,_,_,_=self.fixture();old=self.store.config()
        self.store.save_config({'revision':old['revision'],'model_max_output_tokens':32000,'model_response_max_mib':16})
        self.store.active(job['id'],job['owner'])
        frozen=self.store.job_config(job['id'])
        self.assertEqual(frozen['model_max_output_tokens'],old['model_max_output_tokens']);self.assertEqual(frozen['model_response_max_mib'],8)
        self.assertEqual(self.store.config()['execution_revision'],old['execution_revision'])

    def test_broker_does_not_send_fixed_output_limit_and_preserves_reason(self):
        self.store.save_config({'revision': self.store.config()['revision'], 'model_max_output_tokens': 0})
        job,_,_,_,_=self.fixture();token=capability(self.store,job,'测试模型')
        client=create_broker(self.store).test_client();sent=[]
        def upstream(url,body,key,**kwargs):
            sent.append(body)
            return 200,stream('{"findings":[]}',reason='length')
        with patch('ai_review.broker.endpoint',return_value='http://model.invalid/v1'),patch.object(self.store,'unseal',return_value='私有密钥'),patch('ai_review.broker.fetch_model',side_effect=upstream):
            response=client.post('/v1/chat/completions',json={'model':'测试模型','messages':[{'role':'user','content':'源码'}],'stream':True},headers={'Authorization':'Bearer '+token})
        self.assertEqual(response.status_code,200);self.assertNotIn('max_tokens',sent[0])
        meta=self.store.job(job['id'])['diagnostic']
        self.assertEqual(meta['finish_reasons'],['length']);self.assertFalse(meta['completion_complete'])


if __name__=='__main__':unittest.main()
