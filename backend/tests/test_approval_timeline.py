"""时间线完整性测试保留过滤空页、丢事件和快照变化的拒绝断言。"""
import copy
from types import SimpleNamespace
import unittest

from approval.rules import ApprovalError
from approval.timeline import read_complete, validate_reference


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.reference={'repo_id':1,'number':2,'pr_id':3,'count':105,'max_id':205,'events':[
            {'id':101,'type':22,'review_id':11,'ordinal':1},
            {'id':205,'type':29,'review_id':0,'ordinal':105}]}
        self.pages={1:[{'id':101,'type':'review','review_id':11}],2:None,
                    3:[{'id':205,'type':'pull_push','body':'{"is_force_push":true}'}]}
        self.calls=[]

    def read(self, after=None, headers=None, reviews=None, page_size=50):
        reader=SimpleNamespace(total=None)
        def request(path, params):
            self.calls.append(params['page'])
            value=self.pages.get(params['page'])
            reader.total=len(value or []) if headers is None else headers
            return value
        reader.request=request
        values=iter([self.reference,self.reference if after is None else after])
        return read_complete(reader,'/隔离时间线',lambda:next(values),repo_id=1,number=2,pr_id=3,
                             reviews=[{'id':11}] if reviews is None else reviews,page_size=page_size)

    def test_filtered_middle_page_cannot_hide_force_push(self):
        rows=self.read()
        self.assertEqual([r['id'] for r in rows],[101,205])
        self.assertEqual(self.calls,[1,3])

    def test_missing_short_or_empty_relevant_page_is_rejected(self):
        for value in (None,[],[{'id':202,'type':'comment'}]):
            with self.subTest(缺失页=value),self.assertRaises(ApprovalError):
                self.pages[3]=value;self.read()

    def test_forged_type_wrong_review_and_unexpected_event_rejected(self):
        for event in ({'id':101,'type':'comment'}, {'id':101,'type':'review','review_id':12},
                      {'id':102,'type':'pull_push','body':'{}'}):
            with self.subTest(异常事件=event),self.assertRaises(ApprovalError):
                self.pages[1]=[event];self.read()

    def test_metadata_change_during_read_rejected(self):
        after=copy.deepcopy(self.reference);after['count']+=1;after['max_id']+=1
        with self.assertRaises(ApprovalError):self.read(after=after)

    def test_api_review_list_must_contain_referenced_review(self):
        with self.assertRaises(ApprovalError):self.read(reviews=[])

    def test_header_and_duplicates_rejected(self):
        with self.assertRaises(ApprovalError):self.read(headers=105)
        self.pages[3].insert(0,self.pages[1][0])
        with self.assertRaises(ApprovalError):self.read()

    def test_invalid_identity_ordinal_and_scale_rejected(self):
        for key,value in (('repo_id',True),('number',1),('pr_id',4),('count',5001),('max_id',0)):
            item=copy.deepcopy(self.reference);item[key]=value
            with self.subTest(字段=key),self.assertRaises(ApprovalError):validate_reference(item,1,2,3)
        for key,value in (('ordinal',0),('ordinal',106),('review_id',0),('type',True)):
            item=copy.deepcopy(self.reference);item['events'][0][key]=value
            with self.subTest(事件字段=key),self.assertRaises(ApprovalError):validate_reference(item,1,2,3)

    def test_empty_and_smaller_native_page_limit(self):
        self.reference.update(count=0,max_id=0,events=[])
        self.assertEqual(self.read(reviews=[]),[])
        self.assertEqual(self.calls,[])
        self.setUp()
        self.pages[5]=self.pages.pop(3)
        self.assertEqual(len(self.read(page_size=25)),2)
        self.assertEqual(self.calls,[1,5])


if __name__=='__main__':unittest.main(verbosity=2)
