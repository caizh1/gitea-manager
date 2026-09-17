"""固定版本输入、独立根因定位与反证文案的回归验证。"""
import copy
import unittest
from test_ai_review import example
from ai_review.snapshot import work_items
from ai_review.engine import candidates, confirm
from ai_review.evidence import revalidate_location


class QualityTests(unittest.TestCase):
    def unit(self):
        before = 'void save(int x) {\n  int snapshot = x;\n  persist(snapshot);\n}\n'
        after = 'void save(int x) {\n  int snapshot = x + 0;\n  discard(snapshot);\n}\n'
        _, _, snapshot = example(before, after)
        unit = work_items(snapshot)[0]
        anchor = next(a for a in unit['anchors'] if a['side']=='new' and a['line']==3)
        root = next(o for o in unit['operations'] if o['side']=='new' and o['kind']=='call_expression')
        row = dict(anchor_id=anchor['id'],quote=anchor['quote'],operation_id=root['id'],severity='P1',title='持久化状态丢失',
                   trigger='先创建状态再保存',explanation='保存路径提前丢弃记录',impact='原文中未经证明的影响',recommendation='先持久化',
                   before_behavior='写入持久状态',after_behavior='丢弃记录',trace=['创建状态','保存状态后重启'],
                   support=[{'source_id':unit[v]['source_id'],'quote':unit[v]['code']} for v in ('before','after')])
        return snapshot,unit,row

    def check(self,row,unit,final=None,verified=True):
        parsed=candidates({'findings':[row]})
        checks={'checks':[{'candidate_id':parsed[0]['candidate_id'],'verdict':'confirmed','location_verified':verified,
                           'reason':'按源码复核状态路径','finding':final or row}]}
        return confirm(parsed,checks,unit)

    def test_fixed_direction_one_function_one_unit(self):
        snapshot,unit,_=self.unit()
        self.assertEqual(len(work_items(snapshot)),1)
        self.assertIn('persist(snapshot)',unit['before']['code'])
        self.assertIn('discard(snapshot)',unit['after']['code'])
        self.assertNotIn('code',unit)
        self.assertEqual({a['side'] for a in unit['anchors']},{'old','new'})

    def test_refutation_replaces_all_published_text(self):
        snapshot,unit,row=self.unit()
        fixed=dict(row,impact='仅丢失未提交记录',trigger='契约允许的具体路径')
        accepted,rejected=self.check(row,unit,fixed)
        self.assertFalse(rejected)
        self.assertEqual(accepted[0]['impact'],fixed['impact'])
        self.assertTrue(accepted[0]['anchor'])
        finding=dict(accepted[0],path=unit['path'])
        self.assertTrue(revalidate_location(finding,snapshot))
        finding['location_evidence']=dict(finding['location_evidence'],quote='伪造操作')
        self.assertFalse(revalidate_location(finding,snapshot))

    def test_adjacent_preparation_line_is_not_root(self):
        _,unit,row=self.unit()
        adjacent=next(a for a in unit['anchors'] if a['side']=='new' and a['line']==2)
        row.update(anchor_id=adjacent['id'],quote=adjacent['quote'])
        accepted,_=self.check(row,unit)
        self.assertIsNone(accepted[0]['anchor'])
        self.assertIn('语法操作',accepted[0]['fallback_reason'])

    def test_disagreement_does_not_retarget(self):
        _,unit,row=self.unit()
        final=dict(row,operation_id='另一操作')
        accepted,_=self.check(row,unit,final)
        self.assertIsNone(accepted[0]['anchor'])

    def test_wrong_evidence_and_version_are_uncertain(self):
        for change in ('伪造引用','只给修改后'):
            _,unit,row=self.unit()
            if change=='伪造引用':row['support'][0]['quote']='实际不存在的代码'
            else:row['support']=row['support'][1:]
            accepted,rejected=self.check(row,unit)
            self.assertFalse(accepted)
            self.assertEqual(rejected[0]['verdict'],'uncertain')

    def test_deleted_operation_can_use_old_side(self):
        _,unit,row=self.unit()
        a=next(a for a in unit['anchors'] if a['side']=='old' and a['line']==3)
        o=next(o for o in unit['operations'] if o['side']=='old' and o['kind']=='call_expression')
        row.update(anchor_id=a['id'],quote=a['quote'],operation_id=o['id'])
        accepted,_=self.check(row,unit)
        self.assertEqual(accepted[0]['anchor']['side'],'old')

    def test_shared_state_context_includes_other_entry_points(self):
        old='struct S { int value; };\nvoid boot(struct S *s) { s->value=0; }\nint read(struct S *s) { return s->value; }\nvoid edit(struct S *s) { s->value=1; }\n'
        _,_,snapshot=example(old,old.replace('s->value=1','s->value=2'))
        unit=work_items(snapshot)[0]
        for version in ('before','after'):
            names=[s['role'] for s in unit['context'] if s['version']==version]
            self.assertTrue(any('boot' in n for n in names))
            self.assertTrue(any('read' in n for n in names))
        self.assertFalse(unit['context_gaps'])

    def test_missing_header_does_not_report_complete_coverage(self):
        old='#include "device.h"\nint f(void) { return 0; }\n'
        _,_,snapshot=example(old,old.replace('return 0','return 1'))
        unit=work_items(snapshot)[0]
        self.assertTrue(unit['context_gaps'])
        self.assertFalse(snapshot['coverage']['complete'])

    def test_multiline_operation_is_not_guessed(self):
        old='int f(int x) {\n return call(\n    x,\n    0);\n}\n'
        new=old.replace('    x,','    x+1,').replace('    0);','    1);')
        _,_,snapshot=example(old,new)
        unit=work_items(snapshot)[0]
        _,_,row=self.unit()
        a=next(a for a in unit['anchors'] if a['side']=='new')
        o=next(o for o in unit['operations'] if o['kind']=='call_expression' and o['side']=='new')
        row.update(anchor_id=a['id'],quote=a['quote'],operation_id=o['id'],support=[{'source_id':unit[v]['source_id'],'quote':unit[v]['code']} for v in ('before','after')])
        accepted,_=self.check(row,unit)
        self.assertIsNone(accepted[0]['anchor'])
        self.assertIn('跨越',accepted[0]['fallback_reason'])

    def test_fifty_fixed_positions_through_quality_gate(self):
        for n in range(1, 51):
            old = ''.join('int f%d(void) { return 0; }\n' % i for i in range(55))
            new = old.replace('int f%d(void) { return 0; }' % n, 'int f%d(void) { return 1; }' % n)
            _,_,snapshot=example(old,new)
            unit=work_items(snapshot)[0]
            _,_,row=self.unit()
            a=next(a for a in unit['anchors'] if a['side']=='new')
            o=next(o for o in unit['operations'] if o['side']=='new' and o['kind']=='return_statement')
            row.update(anchor_id=a['id'],quote=a['quote'],operation_id=o['id'],support=[{'source_id':unit[v]['source_id'],'quote':unit[v]['code']} for v in ('before','after')])
            accepted,_=self.check(row,unit)
            self.assertEqual(accepted[0]['anchor']['line'],n+1)
            self.assertTrue(revalidate_location(dict(accepted[0],path=unit['path']),snapshot))

    def test_renamed_source_uses_header_path_from_each_version(self):
        old='#include "device.h"\nint f(void) { return 0; }\n'
        _,_,snapshot=example(old,old.replace('return 0','return 1'))
        snapshot.update(merge_base='旧提交',head='新提交')
        snapshot['files'][0].update(path='new/code.c',old_path='old/code.c',new_path='new/code.c')
        class Client:
            def __init__(self):self.calls=[]
            def file(self,path,sha):self.calls.append((path,sha));return b'int declaration;'
        client=Client();unit=work_items(snapshot,client)[0]
        self.assertEqual(client.calls,[('old/device.h','旧提交'),('new/device.h','新提交')])
        self.assertEqual({(s['path'],s['version']) for s in unit['context'] if s['role']=='头文件'},
                         {('old/device.h','before'),('new/device.h','after')})
