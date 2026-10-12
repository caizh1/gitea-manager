"""超出旧文件、源码、锚点与单元额度的确定性验收。"""
import contextlib
import difflib
import json
import unittest
from unittest.mock import Mock
import test_ai_review as baseline
from test_ai_review_context_access import snapshot
from ai_review.snapshot import capture,work_items,SourceFile
from ai_review.review_state import append_plan,UnitSequence


class LargeLimitsTests(unittest.TestCase):
    setUp=baseline.StoreTests.setUp
    enqueue=baseline.StoreTests.enqueue

    def test_large_sources_are_checkpointed_and_file_count_is_batch_size(self):
        self.enqueue();job=self.store.claim()
        old='/*'+('原始数据' * 200000)+'*/\nint f(int x) { return x; }\n'
        new=old.replace('return x;','return x+1;')
        patches=[]
        for i in range(3):
            path=f'file{i}.c'
            patches.append(f'diff --git a/{path} b/{path}\n'+''.join(difflib.unified_diff(old.splitlines(True),new.splitlines(True),fromfile='a/'+path,tofile='b/'+path)))
        client=Mock();client.version.return_value='1.26.4';client.repo_id=1;client.prefix='/repos/test/code'
        client.pr.return_value={'state':'open','merged':False,'head':{'sha':'b'*40},'base':{'sha':'a'*40},'merge_base':'a'*40}
        client.request.return_value=''.join(patches).encode();client.file.side_effect=lambda path,sha:(old if sha=='a'*40 else new).encode()
        cfg=dict(self.store.config(),max_files=1)
        snap=capture(client,1,cfg,store=self.store,job=job)
        self.assertEqual(len(snap['files']),3);self.assertTrue(snap['coverage']['complete'])
        self.assertTrue(all(isinstance(f,SourceFile) for f in snap['files']))
        self.assertLess(len(json.dumps(snap)),100000)
        self.assertEqual(snap['files'][2]['old'],old)
        self.store.update(job['id'],job['owner'],snapshot=snap)
        restored=self.store.job(job['id'])['snapshot']
        self.assertEqual(restored['files'][0]['new'],new)
        with contextlib.closing(self.store.connect()) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM review_snapshot_sources').fetchone()[0],6)
            self.assertGreater(c.execute('SELECT sum(length(CAST(code AS BLOB))) FROM review_snapshot_sources').fetchone()[0],8*1024*1024)

    def test_more_than_two_thousand_units_are_not_discarded(self):
        old=''.join(f'int item_{i}(int x) {{return x;}}\n' for i in range(2001))
        new=old.replace('return x;','return x+1;')
        snap=snapshot(old,new)
        items=work_items(snap)
        self.assertEqual(len(items),2001)
        self.assertEqual(snap['coverage']['change_evidence'],{'expected':4002,'supplied':4002})
        self.assertFalse(any(g.get('code')=='UNIT_LIMIT' for g in snap['coverage']['skipped']))

    def test_disk_plan_is_iterated_in_short_pages_and_can_resume(self):
        self.enqueue();job=self.store.claim();position=0
        for batch in range(5):
            values=[{'path':f'file{batch}.c','scope':{'name':str(i)},'context':[]} for i in range(25)]
            position=append_plan(self.store,job,values,position)
        rows=UnitSequence(self.store,job['id'])
        self.assertEqual(len(rows),125)
        self.assertEqual([r['position'] for r in rows],list(range(125)))
        self.assertEqual(self.store.job(job['id'],evidence=False)['call_limit'],-1)


if __name__=='__main__':unittest.main()
