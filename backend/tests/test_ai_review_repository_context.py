"""主动检索的固定版本、预算、精确原文和断点回执验收；模型协议替身与真实模型分开。"""
import contextlib
import hashlib
import json
import unittest
from unittest.mock import patch

import test_ai_review as baseline
from test_ai_review_context_access import snapshot
from ai_review.common import ReviewError, Stale, encode
from ai_review.engine import review
from ai_review.repository_context import RepositoryContext, phase
from ai_review.review_state import prepare, units
from ai_review.snapshot import work_items


class Files:
    def __init__(self, files):self.files=files;self.reads=[];self.complete=True
    def file(self,path,sha):
        self.reads.append((path,sha))
        data=self.files.get((path,sha),self.files.get(path))
        if isinstance(data,Exception):raise data
        if data is None:raise ReviewError('读取文件被拒绝或不存在')
        return data.encode()
    def context_tree(self,sha):
        rows={}
        for key,code in self.files.items():
            if isinstance(key,tuple):
                if key[1]!=sha:continue
                key=key[0]
            raw=(code if isinstance(code,str) else '').encode()
            rows[key]={'sha':hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest(),'size':len(raw)}
        return {'files':rows,'complete':self.complete}


class RepositoryContextTests(unittest.TestCase):
    setUp=baseline.StoreTests.setUp
    enqueue=baseline.StoreTests.enqueue

    def fixture(self, files=None):
        self.enqueue();job=self.store.claim()
        snap=snapshot('int f(int x) { return x; }\n','int f(int x) { return x + 1; }\n')
        item=work_items(snap)[0];client=Files(files or {'lib/limit.h':'#define LIMIT 4\n'})
        reader=RepositoryContext(self.store,job,snap,client)
        return job,snap,item,client,reader

    def test_read_fixed_versions_and_original_crlf_unicode(self):
        job,snap,item,client,reader=self.fixture({('lib/limit.h','a'*40):'// 中文\r\n#define LIMIT 4\r\n',
                                                ('lib/limit.h','b'*40):'// 中文\r\n#define LIMIT 8\r\n'})
        for version,wanted in (('before','4'),('after','8')):
            value=reader.execute({'op':'read_file','version':version,'path':'lib/limit.h','start_line':2,'end_line':2})
            self.assertEqual(value['source']['code'],'#define LIMIT '+wanted+'\r')
            self.assertEqual(value['source']['commit'],snap['merge_base' if version=='before' else 'head'])
            self.assertEqual(value['source']['start'],2)
        self.assertEqual(client.reads,[('lib/limit.h','a'*40),('lib/limit.h','b'*40)])

    def test_no_commands_urls_external_paths_or_arbitrary_refs(self):
        _,_,_,client,reader=self.fixture()
        bad=[{'op':'shell','version':'after','command':'id'},
             {'op':'read_file','version':'main','path':'lib/limit.h'},
             {'op':'read_file','version':'after','path':'../secret'},
             {'op':'read_file','version':'after','path':'/etc/passwd'},
             {'op':'read_file','version':'after','path':'lib/limit.h','url':'http://localhost'},
             {'op':'read_file','version':'after','path':'lib/limit.h','start_line':0,'end_line':201}]
        for request in bad:
            with self.subTest(输入=request),self.assertRaises(ReviewError):reader.execute(request)
        self.assertEqual(client.reads,[])

    def test_search_pages_include_all_matches_within_same_file(self):
        _,_,item,_,reader=self.fixture({'lib/limit.h':'\n'.join(f'LIMIT_{i}' for i in range(125))})
        request={'op':'search_code','version':'after','query':'LIMIT','cursor':0}
        pages=reader.perform('分页',0,[request])
        all_hits=[m['line'] for result in pages for m in result['matches']]
        self.assertEqual(all_hits,list(range(1,126)))
        self.assertTrue(pages[-1]['complete']);self.assertFalse(reader.gaps(item,'分页'))

    def test_skipped_middle_page_never_claims_complete_search(self):
        _,_,item,_,reader=self.fixture({f'src/file_{i:03}.c':'int x;' for i in range(151)})
        with patch.object(reader,'next_request',return_value=None):
            reader.perform('漏页',0,[{'op':'find_files','version':'after','pattern':'*','cursor':0}])
            reader.perform('漏页',1,[{'op':'find_files','version':'after','pattern':'*','cursor':150}])
        self.assertTrue(reader.gaps(item,'漏页'))

    def test_incomplete_index_and_permission_denied_are_explicit(self):
        _,_,item,client,reader=self.fixture({'lib/blocked.h':ReviewError('HTTP 403')})
        client.complete=False
        values=reader.perform('未决',0,[{'op':'search_code','version':'after','query':'LIMIT'},
                                     {'op':'read_file','version':'after','path':'lib/missing.h'}])
        self.assertFalse(values[0]['complete']);self.assertFalse(values[0]['index_complete'])
        self.assertEqual(values[1]['status'],'error');self.assertEqual(len(reader.gaps(item,'未决')),2)

    def test_changed_tree_blob_is_rejected(self):
        _,_,_,client,reader=self.fixture()
        reader.index('after');client.files['lib/limit.h']='#define LIMIT 999\n'
        with self.assertRaisesRegex(ReviewError,'Git 对象'):reader.read('lib/limit.h','after')

    def test_supplied_header_clears_initial_missing_message_but_keeps_other_version(self):
        _,_,item,_,reader=self.fixture()
        item['header_needs']=[{'path':'lib/limit.h','version':v,'reason':'首次读取缺失'} for v in ('before','after')]
        item['context_gaps']=[n['version']+' 头文件尚未读取：'+n['path']+'（'+n['reason']+'）' for n in item['header_needs']]
        reader.perform('补齐',0,[{'op':'read_file','version':'after','path':'lib/limit.h'}])
        effective=reader.augment(item,'补齐')
        self.assertEqual([n['version'] for n in effective['header_needs']],['before'])
        self.assertEqual(len(effective['context_gaps']),1);self.assertTrue(effective['context_gaps'][0].startswith('before'))
        self.assertEqual(len(reader.gaps(effective,'补齐')),1)

    def test_stale_job_cannot_read_even_cached_content(self):
        job,_,_,client,reader=self.fixture()
        reader.read('lib/limit.h','after');self.enqueue('新版本')
        with self.assertRaises(Stale):reader.read('lib/limit.h','after')
        self.assertEqual(len(client.reads),1)

    def test_read_receipt_recovery_does_not_repeat_completed_reads(self):
        job,snap,_,client,reader=self.fixture()
        request={'op':'read_file','version':'after','path':'lib/limit.h'}
        original=reader.perform('单元',0,[request])
        resumed=RepositoryContext(self.store,job,snap,client)
        self.assertEqual(resumed.perform('单元',0,[request]),original)
        self.assertEqual(resumed.perform('单元',1,[request]),original)
        self.assertEqual(len(client.reads),1)

    def test_model_chooses_lookup_then_actual_source_reaches_next_call(self):
        job,snap,item,client,reader=self.fixture();seen=[]
        def model(store,job,config,instruction,payload):
            seen.append(payload)
            if len(seen)==1:return {'context_requests':[{'op':'read_file','version':'after','path':'lib/limit.h'}]}
            self.assertTrue(any(s['code'].startswith('#define LIMIT 4') for s in payload['context']))
            return {'findings':[]}
        result=review(self.store,job,self.store.config(),snap,[item],caller=model,client=client)
        self.assertEqual(len(seen),2);self.assertTrue(result['coverage']['complete'])
        self.assertEqual(result['context_retrieval'],{'rounds':1,'model_rounds':1,'runtime_rounds':0,'operations':1,'unique_requests':1,'failed':0})
        with contextlib.closing(self.store.connect()) as c:
            sent=[json.loads(r[0]) for r in c.execute('SELECT input FROM review_turns ORDER BY seq')]
        self.assertEqual(sent,seen)
        review(self.store,job,self.store.config(),snap,[item],caller=model,client=client)
        self.assertEqual(len(seen),2)

    def test_six_rounds_shared_by_analysis_and_refutation_eight_calls(self):
        job,snap,item,client,reader=self.fixture({f'lib/part_{i}.h':f'int n{i};' for i in range(6)});seen=[]
        proposal={'title':'边界变化','explanation':'需要核对边界','severity':'P2'}
        def model(store,job,config,instruction,payload):
            n=len(seen);seen.append(payload)
            if n in (0,1,2,4,5,6):
                part=n if n<3 else n-1
                return {'context_requests':[{'op':'read_file','version':'after','path':f'lib/part_{part}.h'}]}
            if n==3:return {'findings':[proposal]}
            self.assertNotIn('补查额度已结束',instruction)
            return {'checks':[{'candidate_id':payload['candidates'][0]['candidate_id'],'verdict':'rejected','reason':'相关文件提供了有效检查'}]}
        result=review(self.store,job,self.store.config(),snap,[item],caller=model,client=client)
        self.assertEqual(len(seen),8);self.assertEqual(result['context_retrieval']['rounds'],6)
        self.assertEqual(len(result['rejected']),1);self.assertEqual(result['findings'],[])
        self.assertEqual(self.store.job(job['id'])['call_limit'],-1)

    def test_budget_exhaustion_keeps_provisional_issue_and_reports_unresolved(self):
        job,snap,item,client,_=self.fixture();calls=[]
        def model(store,job,config,instruction,payload):
            calls.append(payload)
            return {'findings':[{'title':'候选问题','explanation':'已观察到行为变化'}],
                    'context_requests':[{'op':'read_file','version':'after','path':'lib/limit.h'}]}
        result=review(self.store,job,self.store.config(),snap,[item],caller=model,client=client)
        self.assertLessEqual(len(calls),8)
        self.assertEqual(result['findings'][0]['title'],'候选问题')
        self.assertFalse(result['coverage']['complete'])
        self.assertTrue(any('连续三次' in g['reason'] for g in result['coverage']['skipped']))

    def test_unknown_call_is_never_replayed(self):
        job,snap,item,client,reader=self.fixture();calls=[]
        def model(*args):calls.append(1);raise ReviewError('模型连接中断，结果不确定')
        for attempt in range(2):
            with self.assertRaises(ReviewError):review(self.store,job,self.store.config(),snap,[item],caller=model,client=client)
        self.assertEqual(len(calls),1)

    def test_resume_after_response_persisted_before_read(self):
        job,snap,item,client,reader=self.fixture();calls=[]
        uid=prepare(self.store,job,[item],8)[0]['id']
        def model(*args):
            calls.append(1)
            return {'context_requests':[{'op':'read_file','version':'after','path':'lib/limit.h'}]} if len(calls)==1 else {'findings':[]}
        with patch.object(reader,'perform',side_effect=RuntimeError('模拟进程退出')):
            with self.assertRaises(RuntimeError):phase(self.store,job,self.store.config(),uid,item,'analyzing','审查',snap,model,reader)
        response,actual=phase(self.store,job,self.store.config(),uid,item,'analyzing','审查',snap,model,reader)
        self.assertEqual(response,{'findings':[]});self.assertEqual(len(calls),2);self.assertEqual(len(client.reads),1)

    def test_more_than_four_operations_are_processed_without_replay(self):
        job,snap,item,client,_=self.fixture()
        def model(*args):return {'context_requests':[{'op':'read_file','version':'after','path':'lib/limit.h'}]*5}
        with self.assertRaisesRegex(ReviewError,'所有分析单元'):
            review(self.store,job,self.store.config(),snap,[item],caller=model,client=client)
        self.assertEqual(len(client.reads),1)
        self.assertFalse(self.store.job(job['id'])['result']['coverage']['complete'])

    def test_search_metadata_cannot_evict_actually_requested_source(self):
        job,snap,item,client,reader=self.fixture({'lib/limit.h':'\n'.join('BOUNDARY_'+str(i)+'_'+'x'*480 for i in range(100))})
        reader.perform('证据优先',0,[{'op':'search_code','version':'after','query':'BOUNDARY'}])
        reader.perform('证据优先',1,[{'op':'read_file','version':'after','path':'lib/limit.h','start_line':65,'end_line':70}])
        actual=reader.augment(item,'证据优先')
        self.assertTrue(any('BOUNDARY_64_' in s['code'] and 'BOUNDARY_69_' in s['code'] for s in actual['context']))
        self.assertLessEqual(len(encode(actual)),48000)

    def test_model_structured_unresolved_reason_cannot_disappear(self):
        from ai_review.repository_context import unresolved
        _,_,item,_,_=self.fixture()
        reasons=unresolved({'unresolved_context':[{'symbol':'read_cell','gap':'读取实现缺失'}]},item)
        self.assertEqual(reasons[0]['reason'],'读取实现缺失')

    def test_initial_context_evicted_by_reads_reports_input_limit(self):
        from ai_review.context import source
        from ai_review.repository_context import unresolved
        job,snap,item,client,reader=self.fixture({f'lib/h{i}.h':'x'*11500 for i in range(3)})
        initial=source(snap,'lib/initial.h','before','相关声明','y'*11000,1)
        item['context']=[initial]
        reader.perform('输入预算',0,[{'op':'read_file','version':'after','path':f'lib/h{i}.h'} for i in range(3)])
        actual=reader.augment(item,'输入预算')
        self.assertIn(initial['source_id'],actual['context_omitted'])
        self.assertTrue(any(g['code']=='CONTEXT_INPUT_LIMIT' for g in unresolved({},actual)))


if __name__=='__main__':unittest.main()
