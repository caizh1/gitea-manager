"""复杂控制流、真实宏和跨版本原文的代表基准；答案不进入审查输入。"""
import json
from pathlib import Path
import runpy
import unittest

from test_ai_review_context_access import snapshot
import test_ai_review_context_access as context_tests
from ai_review.snapshot import work_items

FIXTURES=runpy.run_path(str(Path(__file__).resolve().parents[2]/'scripts/review-context-fixtures.py'))


class ComplexContextTests(unittest.TestCase):
    supplied=context_tests.ScreenshotContextTests.supplied
    def test_compiled_representative_c_cpp_changes_are_all_supplied(self):
        for case in FIXTURES['corpus']():
            with self.subTest(类型=case['name'],缺陷=case['defect']):
                value=snapshot(case['old'],case['new'],case['path'])
                items=self.supplied(value)
                self.assertTrue(items)
                if case['name'] in ('declaration','adjacent','insert'):
                    self.assertGreater(len(case['new'].splitlines()),7000)
                    for item in items:
                        self.assertTrue(item['before']['code']);self.assertTrue(item['after']['code'])
                if case['name']=='cross_file':
                    self.assertTrue(items[0]['context_needs'])
                    self.assertTrue(any(n['symbol'].startswith('slot_') for n in items[0]['context_needs']))

    def test_asymmetric_replacements_and_moved_crlf_chunks(self):
        original='unsigned dispatch(unsigned x) {\n'+''.join(f'  x ^= {i}u;\n' for i in range(7000))+'  return x;\n}\n'
        for left,right in ((11,20),(20,11),(12,13),(13,12)):
            old=original.replace('\n','\r\n')
            removed=''.join(f'  x ^= {i}u;\r\n' for i in range(5901,5901+left))
            added=''.join(f'  x += {i}u;\r\n' for i in range(901,901+right))
            items=self.supplied(snapshot(old,old.replace(removed,added),'renamed/ports/irq.cc'))
            self.assertTrue(all(i['before']['code'] and i['after']['code'] for i in items))

    def test_large_dependencies_select_bound_names_and_preceding_guard(self):
        case=next(c for c in FIXTURES['corpus']() if c['name']=='declaration' and c['defect'])
        items=self.supplied(snapshot(case['old'],case['new'],case['path']))
        for item in items:
            context='\n'.join(s['code'] for s in item['context'])
            self.assertNotIn('u32 local_0',context)
            self.assertLess(len(item['context']),30)
            self.assertTrue(any(s['role']=='相关前置检查' for s in item['context']))

    def test_small_macro_change_keeps_full_users_and_correct_operation_end(self):
        case=next(c for c in FIXTURES['corpus']() if c['name']=='global' and c['defect'])
        for item in self.supplied(snapshot(case['old'],case['new'],case['path'])):
            self.assertEqual(item['before']['code'],case['old'])
            self.assertEqual(item['after']['code'],case['new'])
            macro=next(o for o in item['operations'] if o['kind']=='preproc_def' and '63u' in o['quote'])
            self.assertEqual(macro['start'],macro['end'])
            anchors={a['id']:a for a in item['anchors']}
            self.assertTrue(all(anchors[aid]['quote']!='#endif' for aid in macro['anchor_ids']))
            symbol=next(s for s in item['changed_symbols'] if s['name']=='PIN_MASK')
            self.assertEqual({s['version'] for s in symbol['references']},{'before','after'})
            self.assertTrue(any('1u <<' in s['code'] for s in symbol['references']))
            from ai_review.evidence import source_evidence
            refs=[s for s in symbol['references'] if '1u <<' in s['code']]
            proof,warning=source_evidence({'support':[{'source_id':s['source_id'],'quote':s['code']} for s in refs]},item)
            self.assertTrue(proof);self.assertFalse(warning)

    def test_ten_files_ninety_modified_functions_counts_actual_text(self):
        old,new=FIXTURES['large_pr']();snap=None
        for path in old:
            one=snapshot(old[path],new[path],path)
            if snap is None:snap=one
            else:snap['files'].extend(one['files']);snap['anchors'].update(one['anchors'])
        items=self.supplied(snap)
        self.assertEqual(len(items),90)
        self.assertEqual(snap['coverage']['change_evidence'],{'expected':180,'supplied':180})
        self.assertTrue(snap['coverage']['complete'])


if __name__=='__main__':unittest.main()
